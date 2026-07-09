from __future__ import annotations

import json
import sys
from pathlib import Path

from rich.console import Console
from rich.rule import Rule

from .models import AudioFile, DuplicateGroup

_console = Console()


def _fmt_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024:.0f} KB"


def _fmt_duration(seconds: float) -> str:
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"


def _best_file(group: DuplicateGroup) -> AudioFile:
    def quality_key(f: AudioFile) -> float:
        if f.bitrate > 0:
            return float(f.bitrate)
        return float(f.size)

    return max(group.files, key=quality_key)


def print_report(
    total_scanned: int,
    groups: list[DuplicateGroup],
    warnings: list[str],
) -> None:
    _console.print()
    _console.rule("[bold blue]DUPLICATE AUDIO FILES REPORT[/bold blue]")
    _console.print(
        f"Scanned: [bold]{total_scanned}[/bold] files  |  "
        f"Found: [bold]{len(groups)}[/bold] duplicate groups"
    )

    for w in warnings:
        _console.print(f"\n[yellow][!] {w}[/yellow]")

    total_savings = 0

    confidence_specs = [
        ("high", "HIGH CONFIDENCE — Fingerprint Verified"),
        ("medium", "MEDIUM CONFIDENCE — Fingerprint Verified, Duration Differs"),
    ]

    for confidence, label_text in confidence_specs:
        tier_groups = [g for g in groups if g.confidence == confidence]
        if not tier_groups:
            continue

        _console.print()
        _console.print(Rule(f"[bold]{label_text}[/bold]  ({len(tier_groups)} groups)"))

        for i, group in enumerate(tier_groups, 1):
            title = group.files[0].tags.get("title") or ""
            artist = group.files[0].tags.get("artist") or ""
            label = f'"{title}"' if title else group.files[0].path.name
            if artist:
                label += f" – {artist}"
            dur = _fmt_duration(group.files[0].duration)
            score_str = f"  [dim](score: {group.score:.0%})[/dim]"
            tag_note = (
                "tags/filenames agree"
                if group.tag_score >= 0.5
                else "fingerprint only — tags/filenames differ"
            )

            _console.print(f"\n  Group {i}  {label}  [{dur}]{score_str}  [dim]({tag_note})[/dim]")

            best = _best_file(group)
            sorted_files = sorted(group.files, key=lambda f: f.size, reverse=True)
            group_savings = sum(f.size for f in sorted_files) - best.size
            total_savings += group_savings

            for f in sorted_files:
                keep = "  [green]← keep[/green]" if f is best else ""
                _console.print(f"    {f.path}  {_fmt_size(f.size)}{keep}")

    _console.print()
    _console.rule()
    _console.print(f"Total potential savings: [bold]~{_fmt_size(total_savings)}[/bold]")
    _console.print()


def print_json(groups: list[DuplicateGroup], output_path: Path | None = None) -> None:
    output = [
        {
            "tier": g.tier,
            "confidence": g.confidence,
            "score": round(g.score, 4),
            "tag_score": round(g.tag_score, 4),
            "files": [str(f.path) for f in g.files],
            "keep": str(_best_file(g).path),
        }
        for g in groups
    ]
    # ensure_ascii=False keeps non-ASCII tag/filename characters as actual UTF-8
    # text instead of \uXXXX escapes.
    text = json.dumps(output, indent=2, ensure_ascii=False)
    if output_path is None:
        # Writing raw encoded bytes (rather than print(), whose text-mode
        # stdout encoding depends on the console's codepage) guarantees the
        # output is UTF-8 regardless of platform/locale.
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.write(b"\n")
    else:
        output_path.write_text(text + "\n", encoding="utf-8")
