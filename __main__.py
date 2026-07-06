from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .cache import FingerprintCache
from .matchers.fingerprint import find_fingerprint_duplicates
from .matchers.names import find_name_duplicates
from .matchers.tags import find_tag_duplicates
from .reporter import print_json, print_report
from .scanner import scan


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m audio_dedup",
        description="Find duplicate audio files using tags, fingerprints, and filenames.",
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
        "--no-fingerprint",
        action="store_true",
        help="Skip acoustic fingerprint tier",
    )
    parser.add_argument(
        "--no-names",
        action="store_true",
        help="Skip filename similarity tier",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output machine-readable JSON",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to fingerprint cache file (default: .audio_dedup_cache.json beside scanned dir)",
    )
    args = parser.parse_args()

    directory = args.directory.resolve()
    if not directory.is_dir():
        print(f"Error: {directory} is not a directory", file=sys.stderr)
        return 1

    cache_path = args.cache or directory.parent / ".audio_dedup_cache.json"
    cache = FingerprintCache(cache_path)

    if not args.json_output:
        print(f"Scanning {directory} ...")
    files = scan(directory, min_size=args.min_size * 1024)
    if not args.json_output:
        print(f"Found {len(files)} audio files")

    all_groups = []
    warnings: list[str] = []
    matched: set = set()

    # Tier 1 — tags
    tag_groups, tag_matched = find_tag_duplicates(files)
    matched |= tag_matched
    all_groups.extend(tag_groups)

    # Tier 2 — fingerprint
    if not args.no_fingerprint:
        remaining = [f for f in files if f.path not in matched]
        fp_groups, fp_matched, fp_warnings = find_fingerprint_duplicates(remaining, cache)
        matched |= fp_matched
        all_groups.extend(fp_groups)
        warnings.extend(fp_warnings)

    # Tier 3 — filename
    if not args.no_names:
        remaining = [f for f in files if f.path not in matched]
        name_groups, _ = find_name_duplicates(remaining)
        all_groups.extend(name_groups)

    if args.json_output:
        print_json(all_groups)
    else:
        print_report(len(files), all_groups, warnings)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
