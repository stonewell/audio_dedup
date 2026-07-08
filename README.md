# audio_dedup

Find duplicate audio files in a large collection by acoustic content, not by
metadata.

## Install

```
pip install audio_dedup                 # or: pip install -e tools/audio_dedup for development
pip install "audio_dedup[fingerprint]"   # adds the library-based fingerprinting backend
```

## Usage

```
audio-dedup <directory> [--min-size KB] [--json] [--cache PATH] [--workers N] [--force-refresh]
```

`python -m audio_dedup <directory> ...` also works, from an environment where the
package is installed.

`--force-refresh` recomputes the fingerprint for every file, ignoring the
cache — use it if you suspect a cached fingerprint is stale for a reason the
cache can't detect (e.g. the file's content changed without its mtime
changing). Without it, only files whose cache entry is missing or whose
mtime has changed are recomputed — a normal re-run of a large collection
only touches what's new or edited.

## Design

Duplicate detection is **fingerprint-first**: every file is acoustic-fingerprinted
(via [Chromaprint](https://acoustid.org/chromaprint)), and a pair of files is a
duplicate only when their fingerprints actually match. Tags and filenames are
never required to agree and never gate whether a fingerprint check happens —
they're only used afterwards, to annotate a *confirmed* match as "tags agree"
or "fingerprint-only" in the report. This matters because the most common
real-world duplicates — a track ripped twice, downloaded from two sources, or
re-encoded — frequently have no tag or filename correlation at all (missing
tags, hashed/randomized filenames, translated titles). Gating fingerprinting
behind a metadata-similarity threshold, as an earlier version of this tool
did, misses exactly the duplicates that matter most.

Comparing every file against every other file is O(n²) and doesn't scale past
a few thousand files. Instead:

1. **Scan** (`scanner.py`) reads tags for every file in parallel
   (`ProcessPoolExecutor`) — tag parsing spends real time in pure-Python code
   (struct unpacking, frame decoding), so a thread pool mostly serializes on
   the GIL instead of scaling with cores; a process pool actually does.
2. **Fingerprint** (`matchers/fingerprint.py`) computes an acoustic fingerprint
   for every file, in parallel (`ProcessPoolExecutor`, sized to CPU count).
   Fingerprints are cached by `(path, mtime)` in a SQLite database
   (`cache.py`), so repeat runs only fingerprint files that changed.
3. **Index** (`matchers/indexing.py`, `cache.py`) generates candidate pairs via
   an inverted index of coarse fingerprint terms, modeled on how AcoustID's
   own server indexes Chromaprint output, instead of comparing durations.
   Each 32-bit chromaprint value is quantized 4 ways (each dropping a
   different 8-bit octet, so a handful of re-encode bit-flips still leaves at
   least one view matching) and stored as `(term, file_id)` postings in
   SQLite. To find candidates for a file, its own terms are looked up in the
   index and files sharing enough of them (containment ≥10% of the smaller
   file's term count, floor of 20 shared terms) become candidates — this is
   duration-agnostic, so it also catches cases duration-window blocking
   structurally couldn't: a duplicate with an added intro/outro (shifts
   total duration), or a preview clip fully contained in a longer track.
   Overly common terms (e.g. a near-silent intro shared by unrelated songs)
   are excluded above a document-frequency cap so one ubiquitous value can't
   flood every file's candidate list.
4. **Verify** (`matchers/fingerprint.py: fingerprint_similarity`) computes
   Hamming similarity between candidate pairs' fingerprints, searching a
   small range of frame offsets (±20 frames, ~2.5s) before giving up. A naive
   index-aligned comparison misses matches where one file has extra lead-in
   silence or a slightly different trim — a fraction-of-a-second shift is
   enough to desync a frame-by-frame comparison otherwise. This step alone
   decides accept/reject; the index above only decides what's worth checking.
5. **Cluster** (`matchers/duplicates.py`) unions confirmed pairs (≥0.85
   similarity) via union-find into duplicate groups, and separately computes
   two non-gating annotations for the report only: a corroborating
   tag/filename fuzzy score (`matchers/identity.py`), and a confidence level
   (`"medium"` instead of `"high"` if a confirmed pair's durations differ by
   more than 5s — still a real fingerprint match, just worth a second glance,
   e.g. a partial/edited match).

## Why these specific choices

- **SQLite over JSON for the fingerprint cache**: the old JSON cache
  rewrote the entire file on every flush — O(cache size) I/O per write. For a
  collection large enough to matter, that cache file itself becomes
  hundreds of MB, and re-serializing it every 25 fingerprints degrades a scan
  to quadratic I/O. SQLite (stdlib `sqlite3`, no new dependency) gives
  incremental, indexed writes instead — each fingerprint is committed once,
  in O(1).
- **Inverted index over duration-window blocking**: an earlier version of
  this tool used a duration-sorted sliding window instead. It was cheap, but
  structurally blind to any duplicate whose total duration differs by more
  than the tolerance — an added intro/outro, or a preview clip vs. the full
  track. Indexing fingerprint *content* rather than file *duration* catches
  both, and is how Chromaprint's own authors designed it to be matched at
  scale (AcoustID's server does the same thing: index terms, candidate, then
  verify).
- **Octet-masked multi-view terms over raw fingerprint values**: indexing
  raw 32-bit values verbatim would be a much smaller change, but silently
  loses recall if per-frame re-encode noise touches the bits you happened to
  pick. Masking off one octet at a time and indexing all 4 views means a
  handful of scattered bit-flips almost always leaves at least one view
  intact, without having to know in advance where chromaprint's noise
  concentrates.
- **Containment over Jaccard for the candidacy threshold**: Jaccard
  (shared / union) penalizes size mismatch, which is exactly wrong for a
  short clip fully contained in a much longer file — the union is dominated
  by the long file's exclusive terms, driving Jaccard toward zero even for a
  perfect partial match. Containment against the smaller file's own term
  count asks the right question: how much of the smaller file's content is
  present in the other one.
- **Process pool over thread pool for fingerprinting and scanning**: both
  spend real time in Python-level work (decode loops; struct/frame parsing)
  rather than blocking on I/O the GIL would release, so a thread pool
  wouldn't get full parallelism across cores — confirmed by an earlier
  version of the scanner using a thread pool and not scaling with core
  count. A process pool does, at the cost of small per-task overhead that's
  negligible next to actual decode/parse time.
- **No skip-fingerprint flag**: since fingerprinting is the only signal that
  determines a duplicate, an option to skip it would just make the tool find
  nothing. Earlier iterations of this tool had `--no-fingerprint` /
  `--no-names` flags from when tags/filenames were separate matching tiers;
  those tiers no longer exist.

## Requirements

One fingerprinting backend is required (library preferred, falls back to
binary automatically):

```
pip install "audio_dedup[fingerprint]"   # library path (chromaprint + audioread)
# or: install fpcalc from https://acoustid.org/chromaprint and put it on PATH
```

Core dependencies (`mutagen`, `rapidfuzz`, `rich`) are installed automatically
by `pip install`; see `pyproject.toml` for exact versions.
