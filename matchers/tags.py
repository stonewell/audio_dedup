from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from ..models import AudioFile, DuplicateGroup

_FEAT_RE = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)
_NONWORD_RE = re.compile(r"[^\w\s]")


def _norm(s: str) -> str:
    s = s.lower()
    s = _NONWORD_RE.sub("", s)
    return " ".join(s.split())


def _norm_artist(s: str) -> str:
    s = _FEAT_RE.sub("", s)
    return _norm(s)


def find_tag_duplicates(
    files: list[AudioFile],
    duration_tolerance: float = 3.0,
) -> tuple[list[DuplicateGroup], set[Path]]:
    buckets: dict[tuple[str, str], list[AudioFile]] = defaultdict(list)
    for f in files:
        title = _norm(f.tags.get("title", ""))
        artist = _norm_artist(f.tags.get("artist", ""))
        if title and artist:
            buckets[(title, artist)].append(f)

    groups: list[DuplicateGroup] = []
    matched: set[Path] = set()

    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        # Cluster by duration proximity
        used = [False] * len(bucket)
        for i in range(len(bucket)):
            if used[i]:
                continue
            cluster = [bucket[i]]
            for j in range(i + 1, len(bucket)):
                if not used[j] and abs(bucket[i].duration - bucket[j].duration) <= duration_tolerance:
                    cluster.append(bucket[j])
                    used[j] = True
            if len(cluster) >= 2:
                used[i] = True
                groups.append(DuplicateGroup(tier="tags", confidence="high", files=cluster, score=1.0))
                for f in cluster:
                    matched.add(f.path)

    return groups, matched
