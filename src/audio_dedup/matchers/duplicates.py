from __future__ import annotations

from pathlib import Path

from ..cache import FingerprintCache
from ..models import AudioFile, DuplicateGroup
from .fingerprint import ensure_fingerprints, fingerprint_similarity
from .identity import identity_score
from .indexing import extract_terms, is_candidate

MIN_SHARED_TERMS = 20     # absolute floor on shared terms before a pair is even considered
MIN_SHARED_FRACTION = 0.10  # containment threshold against the smaller file's own term count
TERM_DF_CAP = 2000        # terms present in more files than this are too common to be useful
CONFIDENCE_DURATION_TOLERANCE = 5.0  # seconds; used only to annotate confidence, never to gate


def find_duplicates(
    files: list[AudioFile],
    cache: FingerprintCache,
    fingerprint_threshold: float = 0.85,
    max_workers: int | None = None,
    verbose: bool = False,
    force_refresh: bool = False,
) -> tuple[list[DuplicateGroup], set[Path], list[str]]:
    """Cluster files into duplicate groups using acoustic fingerprints.

    Every file is fingerprinted (cached across runs, so repeat scans are
    incremental unless force_refresh forces a full recompute). Candidate
    pairs come from an inverted index of coarse fingerprint terms (see
    matchers/indexing.py and cache.py's terms/term_stats tables) instead of
    duration-proximity blocking — content-based candidacy catches cases
    duration windows structurally can't, like a duplicate with an added
    intro/outro (shifts duration) or a preview clip fully contained in a
    longer track. A pair is only accepted as a duplicate once their
    fingerprints are actually similar — tag/filename agreement is never
    required and never skips the fingerprint check. Tag/filename similarity
    is computed only for fingerprint-confirmed pairs, purely to annotate the
    report.
    """
    warnings, new_count = ensure_fingerprints(
        files, cache, max_workers=max_workers, verbose=verbose, force_refresh=force_refresh
    )
    backfilled = cache.backfill_terms(verbose=verbose)
    if new_count or backfilled:
        cache.rebuild_term_stats()

    parent: dict[Path, Path] = {f.path: f.path for f in files}

    def find(x: Path) -> Path:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: Path, y: Path) -> None:
        parent[find(x)] = find(y)

    files_by_path_str = {str(f.path): f for f in files}
    edge_score: dict[frozenset[Path], float] = {}
    edge_tag_score: dict[frozenset[Path], float] = {}
    edge_duration_delta: dict[frozenset[Path], float] = {}

    eligible = [f for f in files if f.fingerprint]
    total = len(eligible)
    matches_found = 0

    if verbose and total > 0:
        print(f"  Matching {total} files against the fingerprint index...", flush=True)

    for i, a in enumerate(eligible, 1):
        a_terms = extract_terms(a.fingerprint)
        candidates = cache.find_candidates(str(a.path), a_terms, MIN_SHARED_TERMS, TERM_DF_CAP)
        for cand_path_str, shared, cand_term_count in candidates:
            if not is_candidate(shared, len(a_terms), cand_term_count, MIN_SHARED_TERMS, MIN_SHARED_FRACTION):
                continue
            b = files_by_path_str.get(cand_path_str)
            if b is None or not b.fingerprint:
                continue
            sim = fingerprint_similarity(a.fingerprint, b.fingerprint, fingerprint_threshold)
            if sim >= fingerprint_threshold:
                union(a.path, b.path)
                key = frozenset((a.path, b.path))
                edge_score[key] = sim
                edge_tag_score[key] = identity_score(a, b) / 100.0
                edge_duration_delta[key] = abs(a.duration - b.duration)
                matches_found += 1
        if verbose:
            print(f"  [{i}/{total}] {matches_found} matches\r", end="", flush=True)

    if verbose and total > 0:
        print(f"  Done: {matches_found} matches" + " " * 20)

    buckets: dict[Path, list[AudioFile]] = {}
    for f in files:
        root = find(f.path)
        buckets.setdefault(root, []).append(f)

    groups: list[DuplicateGroup] = []
    matched: set[Path] = set()

    for members in buckets.values():
        if len(members) < 2:
            continue
        member_paths = {m.path for m in members}
        cluster_scores = [s for key, s in edge_score.items() if key <= member_paths]
        cluster_tag_scores = [s for key, s in edge_tag_score.items() if key <= member_paths]
        cluster_deltas = [d for key, d in edge_duration_delta.items() if key <= member_paths]
        avg_score = sum(cluster_scores) / len(cluster_scores) if cluster_scores else 1.0
        avg_tag_score = sum(cluster_tag_scores) / len(cluster_tag_scores) if cluster_tag_scores else 0.0
        confidence = "high" if not cluster_deltas or max(cluster_deltas) <= CONFIDENCE_DURATION_TOLERANCE else "medium"
        groups.append(DuplicateGroup(
            tier="fingerprint",
            confidence=confidence,
            files=members,
            score=avg_score,
            tag_score=avg_tag_score,
        ))
        for f in members:
            matched.add(f.path)

    return groups, matched, warnings
