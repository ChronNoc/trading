"""Shared decision-record structures consumed by the assistant agents."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedCondition:
    """One explanation line parsed back into a pass/fail condition."""

    passed: bool
    message: str


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One finalized setup decision captured for narration and review."""

    recorded_at: str
    setup_name: str
    decision: str
    explanations: tuple[str, ...]
    regime: str = ""

    @property
    def accepted(self) -> bool:
        """Return true when the decision was an acceptance."""
        return self.decision.casefold() == "accepted"


_LINE_PATTERN = re.compile(r"^\[(pass|fail)\]\s*(.+)$")

_CONDITION_NAME_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("important level", "price at"), "At important level"),
    (("aggressive", "sold"), "Aggressive sell volume"),
    (("progress", "ticks lower", "moved only"), "Limited downward progress"),
    (("reload", "bid liquidity"), "Bid reload"),
    (("ask cancellation", "ask pull"), "Ask pull ratio"),
    (("reclaim",), "Level reclaim"),
    (("news",), "News lockout"),
)


def parse_explanation_lines(lines: tuple[str, ...]) -> tuple[ParsedCondition, ...]:
    """Parse rendered ``[pass]/[fail]`` explanation lines into conditions.

    Lines that do not carry a pass/fail marker (such as the heading line
    produced by ``SetupEvaluationResult.render_lines``) are skipped.
    """
    parsed: list[ParsedCondition] = []
    for line in lines:
        match = _LINE_PATTERN.match(line.strip())
        if match is None:
            continue
        parsed.append(ParsedCondition(passed=match.group(1) == "pass", message=match.group(2).strip()))
    return tuple(parsed)


def canonical_condition_name(message: str) -> str:
    """Map a dynamic condition message onto a stable display name.

    Condition messages embed measured values ("450 contracts sold
    aggressively"), so the raw text cannot key a history matrix. This
    reduces each message to the strategy condition it describes.
    """
    lowered = message.casefold()
    for keywords, name in _CONDITION_NAME_RULES:
        if any(keyword in lowered for keyword in keywords):
            return name
    words = message.split()
    return " ".join(words[:4]) if words else "Unknown condition"
