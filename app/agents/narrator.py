"""Deterministic plain-English narration of strategy decisions.

The narrator never decides anything. It re-tells, in trading language,
a decision the deterministic strategy engine already made, using only
the rendered pass/fail explanation lines as input.
"""

from __future__ import annotations

from app.agents.records import (
    DecisionRecord,
    ParsedCondition,
    canonical_condition_name,
    parse_explanation_lines,
)

_PASS_PHRASES: dict[str, str] = {
    "At important level": "price was sitting at a meaningful level",
    "Aggressive sell volume": "heavy aggressive selling hit the book",
    "Limited downward progress": "all that selling barely pushed price lower",
    "Bid reload": "buyers kept reloading the bid at the level",
    "Ask pull ratio": "sellers pulled their offers away from the market",
    "Level reclaim": "price reclaimed the defended level",
    "News lockout": "no news lockout was active",
}

_FAIL_PHRASES: dict[str, str] = {
    "At important level": "price was not at a meaningful level",
    "Aggressive sell volume": "selling never reached the required intensity",
    "Limited downward progress": "price kept sliding, so the sellers were winning",
    "Bid reload": "nobody reloaded the bid, so the level was not truly defended",
    "Ask pull ratio": "the offers stayed stacked overhead",
    "Level reclaim": "price never reclaimed the defended level",
    "News lockout": "a news lockout window was active",
}


def _phrase(condition: ParsedCondition) -> str:
    """Translate one parsed condition into a trading-language phrase."""
    name = canonical_condition_name(condition.message)
    table = _PASS_PHRASES if condition.passed else _FAIL_PHRASES
    return table.get(name, condition.message)


def narrate_decision(setup_name: str, decision: str, explanations: tuple[str, ...]) -> str:
    """Narrate one finalized decision in plain English.

    Returns a short deterministic paragraph. When no pass/fail lines are
    available yet, says so instead of inventing content.
    """
    conditions = parse_explanation_lines(explanations)
    if not conditions:
        return "No finalized decision to narrate yet - run the prototype scenario and a narration will appear here."

    passed = tuple(item for item in conditions if item.passed)
    failed = tuple(item for item in conditions if not item.passed)
    if decision.casefold() == "accepted":
        return (
            f"{setup_name} was ACCEPTED. All {len(conditions)} conditions lined up: "
            + "; ".join(_phrase(item) for item in passed)
            + ". The only action was a hypothetical shadow bracket - no real order exists."
        )
    reasons = "; ".join(_phrase(item) for item in failed)
    looked_right = "; ".join(_phrase(item) for item in passed)
    narration = f"{setup_name} was REJECTED because {reasons}."
    if looked_right:
        narration += (
            f" What did look right: {looked_right}."
            " That combination is a lookalike - exactly the trap these rules exist to filter out."
        )
    return narration


def narrate_record(record: DecisionRecord) -> str:
    """Narrate a captured decision record."""
    return narrate_decision(record.setup_name, record.decision, record.explanations)
