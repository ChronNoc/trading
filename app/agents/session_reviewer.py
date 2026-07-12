"""Deterministic end-of-session review reports built from decision records."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from app.agents import AI_COMMENTARY_LABEL
from app.agents.narrator import narrate_record
from app.agents.records import DecisionRecord, canonical_condition_name, parse_explanation_lines

_REVIEW_QUESTIONS: dict[str, str] = {
    "Bid reload": "Watch the level in the replay: was there truly no resting size refreshing at the bid?",
    "Level reclaim": "Check whether price ever traded back above the defended level within the window.",
    "Aggressive sell volume": "Confirm the tape actually showed the required aggressive volume at the level.",
    "Limited downward progress": "Verify how far price really slid while the selling was hitting.",
    "Ask pull ratio": "Look at the ask side: did offers pull, or did they stay stacked?",
    "At important level": "Re-check that the level qualified as one of the configured important levels.",
    "News lockout": "Confirm whether a lockout window overlapped this decision.",
}


def build_review(records: Sequence[DecisionRecord], *, now: datetime | None = None) -> str:
    """Build a markdown session review from captured decision records."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    accepted = [record for record in records if record.accepted]
    rejected = [record for record in records if not record.accepted]

    lines = [
        "# Session review",
        "",
        f"> {AI_COMMENTARY_LABEL}",
        "> All data in this report is SYNTHETIC prototype data, never real market results.",
        "",
        f"Generated: {stamp}",
        "",
        "## Totals",
        "",
        f"- Decisions recorded: {len(records)}",
        f"- Accepted: {len(accepted)}",
        f"- Rejected: {len(rejected)}",
        "",
    ]

    evaluations: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    for record in records:
        for condition in parse_explanation_lines(record.explanations):
            name = canonical_condition_name(condition.message)
            evaluations[name] += 1
            if not condition.passed:
                failures[name] += 1

    if evaluations:
        lines += [
            "## Condition scoreboard",
            "",
            "| Condition | Evaluations | Failures | Failure rate |",
            "| --- | --- | --- | --- |",
        ]
        for name in sorted(evaluations, key=lambda item: (-failures[item], item)):
            total = evaluations[name]
            failed = failures[name]
            rate = f"{100 * failed // total}%"
            lines.append(f"| {name} | {total} | {failed} | {rate} |")
        lines.append("")

    if failures:
        blocker = max(failures, key=lambda item: (failures[item], item))
        lines += [
            "## Chronic blocker",
            "",
            f"The condition that failed most often was **{blocker}** "
            f"({failures[blocker]} of {evaluations[blocker]} evaluations). "
            "If this rate looks wrong to your eye, that threshold is the first one to revisit.",
            "",
        ]

    lines += ["## Decision log", ""]
    if records:
        for record in records:
            lines.append(f"- `{record.recorded_at}` {narrate_record(record)}")
    else:
        lines.append("- No decisions were recorded this session.")
    lines.append("")

    asked = [
        f"- {question}"
        for name, question in _REVIEW_QUESTIONS.items()
        if failures.get(name, 0) > 0
    ]
    if asked:
        lines += ["## Questions to verify by eye", "", *asked, ""]

    return "\n".join(lines)


def write_review(
    records: Sequence[DecisionRecord],
    output_root: Path,
    *,
    now: datetime | None = None,
) -> Path:
    """Write the markdown review under the given report root and return its path."""
    moment = now or datetime.now(timezone.utc)
    directory = output_root / moment.strftime("%Y-%m-%d")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"ai_review_{moment.strftime('%H%M%S')}.md"
    path.write_text(build_review(records, now=moment), encoding="utf-8")
    return path
