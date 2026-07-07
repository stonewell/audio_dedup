from __future__ import annotations

import math

# Each 32-bit chromaprint sub-fingerprint is quantized 4 ways, each dropping a
# different 8-bit octet. Two encodes of the same audio aren't always bit-identical
# frame-by-frame (minor re-encode noise), so masking off one octet at a time means
# a handful of scattered bit-flips anywhere in the word still leaves at least one
# view unaffected, without having to assume which bits are the noisy ones.
_OCTET_MASKS = (
    0xFFFFFF00,  # view 0: drop bits 0-7
    0xFFFF00FF,  # view 1: drop bits 8-15
    0xFF00FFFF,  # view 2: drop bits 16-23
    0x00FFFFFF,  # view 3: drop bits 24-31
)


def extract_terms(fingerprint: list[int], stride: int = 1) -> set[int]:
    """Extract a set of index terms from a chromaprint fingerprint.

    Each sampled sub-fingerprint is quantized 4 different ways (see
    _OCTET_MASKS) and each (view, masked_value) pair is packed into a single
    64-bit key. Returning a `set` means a long run of near-identical frames
    (e.g. shared silence) contributes at most 4 terms total, not one per
    frame, which bounds how much two unrelated files can spuriously share
    just by both containing quiet intros.
    """
    terms: set[int] = set()
    for i in range(0, len(fingerprint), stride):
        v = fingerprint[i] & 0xFFFFFFFF
        for view_id, mask in enumerate(_OCTET_MASKS):
            terms.add((view_id << 32) | (v & mask))
    return terms


def is_candidate(
    shared: int,
    terms_a: int,
    terms_b: int,
    min_abs: int = 20,
    min_fraction: float = 0.10,
) -> bool:
    """Decide whether a pair sharing `shared` terms is worth verifying with
    fingerprint_similarity(). Containment against the smaller file's own term
    count (not Jaccard against the union) so a short clip fully contained in
    a longer file still scores well regardless of how much longer the other
    file is.
    """
    return shared >= max(min_abs, math.ceil(min_fraction * min(terms_a, terms_b)))
