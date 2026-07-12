"""Offline heuristic extraction of strategy-spec candidates from transcripts.

Scans a ``transcript.json`` (produced by :mod:`video_analysis.transcribe`)
for sentences that mention strategy parameters - stop distances, volume
thresholds, reclaim ticks, and so on - and writes a DRAFT YAML fragment
mapped onto :mod:`app.strategy.spec` field names, with every candidate
citing the transcript timestamp it came from.

This is a deterministic keyword extractor, not a model. Every value it
proposes must be reviewed by a human before it enters any config file;
the tool never writes into config/ and never resolves a spec on its own.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_NUMBER = r"(\d+(?:\.\d+)?)"

_RULE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("exit.stop_method", re.compile(rf"stop[^.]*?{_NUMBER}\s*(?:tick|ticks|point|points)", re.IGNORECASE)),
    ("exit.target_method", re.compile(rf"target[^.]*?{_NUMBER}\s*(?:tick|ticks|point|points)", re.IGNORECASE)),
    (
        "exit.break_even_rule",
        re.compile(rf"break[\s-]?even[^.]*?{_NUMBER}\s*(?:tick|ticks|point|points)", re.IGNORECASE),
    ),
    (
        "entry_setup.aggressive_volume_minimum",
        re.compile(rf"{_NUMBER}\s*(?:contracts|lots)[^.]*?(?:sold|sell|hit|traded|aggress)", re.IGNORECASE),
    ),
    (
        "entry_setup.liquidity_minimum",
        re.compile(rf"(?:bid|liquidity|size)[^.]*?(?:at least|minimum|min)[^.]*?{_NUMBER}", re.IGNORECASE),
    ),
    (
        "entry_setup.maximum_price_progress_ticks",
        re.compile(rf"(?:only|no more than|max(?:imum)?)[^.]*?{_NUMBER}\s*ticks?[^.]*?(?:lower|down|through)", re.IGNORECASE),
    ),
    (
        "entry_setup.reclaim_ticks",
        re.compile(rf"reclaim[^.]*?{_NUMBER}\s*ticks?", re.IGNORECASE),
    ),
    (
        "entry_setup.confirmation_window_ms",
        re.compile(rf"(?:within|confirmation[^.]*?)\s*{_NUMBER}\s*(?:seconds|secs|s)\b", re.IGNORECASE),
    ),
    (
        "exit.time_stop_seconds",
        re.compile(rf"(?:time stop|exit after|flat after)[^.]*?{_NUMBER}\s*(?:seconds|secs|minutes|min)", re.IGNORECASE),
    ),
)


@dataclass(frozen=True, slots=True)
class RuleCandidate:
    """One extracted candidate value with its transcript citation."""

    field: str
    value: str
    start_seconds: float
    end_seconds: float
    quote: str


def extract_candidates(segments: Sequence[dict[str, object]]) -> tuple[RuleCandidate, ...]:
    """Extract spec-field candidates from transcript segments."""
    candidates: list[RuleCandidate] = []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        for field_name, pattern in _RULE_PATTERNS:
            for match in pattern.finditer(text):
                candidates.append(
                    RuleCandidate(
                        field=field_name,
                        value=match.group(1),
                        start_seconds=start,
                        end_seconds=end,
                        quote=text,
                    ),
                )
    return tuple(candidates)


def render_draft_yaml(candidates: Sequence[RuleCandidate]) -> str:
    """Render candidates as a commented DRAFT spec fragment."""
    lines = [
        "# DRAFT strategy-spec candidates extracted from a video transcript.",
        "# Review every value against the video before using any of it.",
        "# This file is NOT a valid config and must never be copied into config/ as-is.",
        "",
    ]
    if not candidates:
        lines.append("# No candidate values were found in this transcript.")
        return "\n".join(lines) + "\n"
    by_field: dict[str, list[RuleCandidate]] = {}
    for candidate in candidates:
        by_field.setdefault(candidate.field, []).append(candidate)
    for field_name in sorted(by_field):
        lines.append(f"{field_name}:")
        for candidate in by_field[field_name]:
            lines.append(
                f"  - value: \"{candidate.value}\"  # at {_format_time(candidate.start_seconds)}",
            )
        lines.append("")
    return "\n".join(lines)


def render_citations(candidates: Sequence[RuleCandidate]) -> str:
    """Render a markdown citation list for manual review."""
    lines = ["# Transcript citations", ""]
    if not candidates:
        lines.append("No candidate values were found in this transcript.")
        return "\n".join(lines) + "\n"
    for candidate in candidates:
        window = f"{_format_time(candidate.start_seconds)}-{_format_time(candidate.end_seconds)}"
        lines.append(f"- **{candidate.field} = {candidate.value}** ({window}): \"{candidate.quote}\"")
    lines.append("")
    return "\n".join(lines)


def run_extraction(transcript_path: Path, output_dir: Path) -> tuple[Path, Path]:
    """Extract candidates and write the draft YAML and citations files."""
    if not transcript_path.is_file():
        raise FileNotFoundError(f"Transcript file not found: {transcript_path}")
    payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    segments = payload.get("segments", []) if isinstance(payload, dict) else []
    candidates = extract_candidates(segments)

    output_dir.mkdir(parents=True, exist_ok=True)
    draft_path = output_dir / "draft_spec_fragment.yaml"
    citations_path = output_dir / "citations.md"
    draft_path.write_text(render_draft_yaml(candidates), encoding="utf-8")
    citations_path.write_text(render_citations(candidates), encoding="utf-8")
    return draft_path, citations_path


def _format_time(seconds: float) -> str:
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes:02d}:{remainder:02d}"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for transcript rule extraction."""
    parser = argparse.ArgumentParser(description="Extract DRAFT strategy-spec candidates from a transcript.")
    parser.add_argument("transcript", type=Path, help="Path to transcript.json from video_analysis.transcribe")
    parser.add_argument("output_dir", type=Path, help="Directory for draft_spec_fragment.yaml and citations.md")
    args = parser.parse_args(argv)
    try:
        draft_path, citations_path = run_extraction(args.transcript, args.output_dir)
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"Draft spec candidates: {draft_path}")
    print(f"Citations: {citations_path}")
    print("Review every value against the video before resolving any spec parameter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
