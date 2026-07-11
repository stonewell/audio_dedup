from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _unique_backup_path(path: Path, suffix: str) -> Path:
    """Pick a backup path that doesn't already exist, appending a numeric
    counter if `<name><suffix>` is already taken (e.g. from a prior run)."""
    candidate = path.with_name(path.name + suffix)
    if not candidate.exists():
        return candidate
    i = 1
    while True:
        candidate = path.with_name(f"{path.name}{suffix}.{i}")
        if not candidate.exists():
            return candidate
        i += 1


def _process_pair(dup_path: Path, keep_path: Path, backup_suffix: str, apply: bool) -> tuple[str, str]:
    """Back up dup_path by renaming it, then hard-link dup_path -> keep_path.

    Returns (status, message), status one of "done", "skip", "fail". Nothing
    is removed — the original duplicate's data survives at the backup path,
    so this is reversible by deleting the new link and renaming the backup back.
    """
    if not dup_path.exists():
        return "skip", f"{dup_path} — already gone"
    if dup_path.samefile(keep_path):
        return "skip", f"{dup_path} — already hard-linked to keep"

    backup_path = _unique_backup_path(dup_path, backup_suffix)

    if not apply:
        return "done", f"{dup_path}  ->  backup {backup_path.name}, then hard-link to {keep_path}"

    dup_path.rename(backup_path)
    try:
        os.link(keep_path, dup_path)
    except OSError as e:
        backup_path.rename(dup_path)  # roll back so the file doesn't just vanish
        return "fail", f"{dup_path} — hard link failed ({e}); backup rolled back"

    return "done", f"{dup_path}  ->  backed up to {backup_path.name}, hard-linked to {keep_path}"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="audio-dedup-apply",
        description="Apply audio_dedup's JSON output: back up each duplicate file "
        "by renaming it, then hard-link it to the file audio_dedup suggested keeping.",
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Path to audio_dedup's JSON output, or '-' (default) to read from stdin",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually rename and hard-link files. Without this, only prints what would happen.",
    )
    parser.add_argument(
        "--backup-suffix",
        default=".bak",
        metavar="SUFFIX",
        help="Suffix appended to a duplicate file's name when backing it up (default: .bak)",
    )
    args = parser.parse_args()

    text = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
    try:
        groups = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON input: {e}", file=sys.stderr)
        return 1

    if not args.apply:
        print("Dry run — pass --apply to actually rename/link files.\n")

    counts = {"done": 0, "skip": 0, "fail": 0}
    for group in groups:
        keep_str = group.get("keep")
        files = group.get("files", [])
        if not keep_str:
            print(f"SKIP  group has no 'keep' file: {files}")
            counts["skip"] += len(files)
            continue

        keep_path = Path(keep_str)
        if not keep_path.exists():
            dup_count = sum(1 for f in files if f != keep_str)
            print(f"SKIP  keep file missing, skipping whole group: {keep_path}")
            counts["skip"] += dup_count
            continue

        for file_str in files:
            if file_str == keep_str:
                continue
            status, message = _process_pair(Path(file_str), keep_path, args.backup_suffix, args.apply)
            print(f"{status.upper():5} {message}")
            counts[status] += 1

    verb = "would be" if not args.apply else "were"
    print(f"\n{counts['done']} {verb} deduplicated, {counts['skip']} skipped, {counts['fail']} failed")
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
