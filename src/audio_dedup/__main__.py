from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .cache import FingerprintCache
from .matchers.duplicates import find_duplicates
from .reporter import print_json, print_report
from .scanner import scan


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="audio-dedup",
        description="Find duplicate audio files by acoustic fingerprint.",
    )
    parser.add_argument("directory", type=Path, help="Directory to scan recursively")
    parser.add_argument(
        "--min-size",
        type=int,
        default=100,
        metavar="KB",
        help="Skip files smaller than N KB (default: 100)",
    )
    parser.add_argument(
        "--json",
        nargs="?",
        const=True,
        default=False,
        metavar="PATH",
        dest="json_output",
        help="Output machine-readable JSON. Use --json=PATH (the '=' is "
        "required — a space-separated path is ambiguous with the directory "
        "argument) to write to a file instead, in which case verbose "
        "progress still prints normally. Plain --json (or PATH omitted) "
        "writes to stdout and suppresses verbose progress so it doesn't mix "
        "with the JSON.",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to fingerprint cache database (default: .audio_dedup_cache.sqlite3 beside scanned dir)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help="Parallel workers for scanning/fingerprinting (default: CPU count)",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Recompute fingerprints for every file, ignoring the cache (default: only changed files are recomputed)",
    )
    args = parser.parse_args()

    directory = args.directory.resolve()
    if not directory.is_dir():
        print(f"Error: {directory} is not a directory", file=sys.stderr)
        return 1

    cache_path = args.cache or directory.parent / ".audio_dedup_cache.sqlite3"
    cache = FingerprintCache(cache_path)
    json_to_stdout = args.json_output is True
    verbose = not json_to_stdout
    workers = args.workers or os.cpu_count() or 4

    if verbose:
        print(f"Scanning {directory} ...")
    files = scan(directory, min_size=args.min_size * 1024, max_workers=workers)
    if verbose:
        cached_entries = len(cache)
        cache_note = f"  (cache: {cache_path}, {cached_entries} entries)"
        print(f"Found {len(files)} audio files{cache_note}")

    all_groups, _matched, warnings = find_duplicates(
        files,
        cache,
        max_workers=workers,
        verbose=verbose,
        force_refresh=args.force_refresh,
    )
    cache.close()

    if args.json_output is False:
        print_report(len(files), all_groups, warnings)
    else:
        output_path = None if json_to_stdout else Path(args.json_output)
        print_json(all_groups, output_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
