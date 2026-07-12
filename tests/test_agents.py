"""Tests for the offline rule-based assistant agents."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

from app.agents import AI_COMMENTARY_LABEL
from app.agents.ask import answer_question
from app.agents.llm_backend import enhance_text, ollama_available
from app.agents.narrator import narrate_decision, narrate_record
from app.agents.records import DecisionRecord, canonical_condition_name, parse_explanation_lines
from app.agents.session_reviewer import build_review, write_review
from app.agents.watchdog import SEVERITY_INFO, SEVERITY_OK, SEVERITY_WARNING, Watchdog
from app.prototype.scenarios import empty_prototype_dashboard_snapshot

ACCEPTED_LINES = (
    "Clean long absorption reclaim: ACCEPTED",
    "[pass] Price at overnight_low",
    "[pass] 450 contracts sold aggressively",
    "[pass] Price moved only 2 ticks lower",
    "[pass] Bid liquidity reloaded 2 times",
    "[pass] Ask cancellation ratio 0.60 met threshold",
    "[pass] Reclaim confirmation completed",
)

REJECTED_LINES = (
    "Rejected lookalike: REJECTED",
    "[pass] Price at overnight_low",
    "[pass] 450 contracts sold aggressively",
    "[fail] Bid liquidity did not reload",
    "[fail] Reclaim confirmation not completed",
)


def _record(decision: str, lines: tuple[str, ...], *, at: str = "14:32:00") -> DecisionRecord:
    return DecisionRecord(
        recorded_at=at,
        setup_name="Long absorption reclaim",
        decision=decision,
        explanations=lines,
    )


def test_parse_explanation_lines_skips_heading() -> None:
    """Only [pass]/[fail] lines become conditions; headings are skipped."""
    parsed = parse_explanation_lines(REJECTED_LINES)

    assert len(parsed) == 4
    assert parsed[0].passed is True
    assert parsed[2].passed is False
    assert parsed[2].message == "Bid liquidity did not reload"


def test_canonical_condition_names_are_stable_across_measured_values() -> None:
    """Messages with different measured values map to the same condition name."""
    assert canonical_condition_name("450 contracts sold aggressively") == "Aggressive sell volume"
    assert canonical_condition_name("12 contracts sold aggressively") == "Aggressive sell volume"
    assert canonical_condition_name("Bid liquidity reloaded 2 times") == "Bid reload"
    assert canonical_condition_name("Bid liquidity did not reload") == "Bid reload"
    assert canonical_condition_name("Reclaim confirmation not completed") == "Level reclaim"
    assert canonical_condition_name("Price at overnight_low") == "At important level"


def test_narrator_accepted_mentions_shadow_only_action() -> None:
    """Accepted narration lists passing conditions and disclaims real orders."""
    text = narrate_decision("Clean long absorption reclaim", "ACCEPTED", ACCEPTED_LINES)

    assert "ACCEPTED" in text
    assert "reloading the bid" in text
    assert "no real order exists" in text


def test_narrator_rejected_names_the_failed_conditions() -> None:
    """Rejected narration leads with the failure reasons."""
    text = narrate_decision("Rejected lookalike", "REJECTED", REJECTED_LINES)

    assert "REJECTED" in text
    assert "nobody reloaded the bid" in text
    assert "never reclaimed" in text
    assert "lookalike" in text


def test_narrator_without_conditions_reports_nothing_to_narrate() -> None:
    """No conditions means the narrator says so instead of inventing text."""
    text = narrate_decision("waiting", "none", ())

    assert "No finalized decision" in text


def test_watchdog_reports_not_running_then_healthy() -> None:
    """The watchdog distinguishes idle, healthy, and warning states."""
    watchdog = Watchdog()
    idle = empty_prototype_dashboard_snapshot()

    assert watchdog.evaluate(idle).severity == SEVERITY_INFO

    healthy = dataclasses.replace(idle, runtime_state="playing", depth_events=10, trade_events=5)
    assert watchdog.evaluate(healthy).severity == SEVERITY_OK


def test_watchdog_warns_on_reconnect_and_data_gap() -> None:
    """Feed gaps produce warnings that mention blocked decisions."""
    watchdog = Watchdog()
    base = empty_prototype_dashboard_snapshot()

    reconnecting = dataclasses.replace(base, runtime_state="playing", synthetic_status="reconnecting")
    report = watchdog.evaluate(reconnecting)
    assert report.severity == SEVERITY_WARNING
    assert "disconnected" in report.message

    gap = dataclasses.replace(base, runtime_state="playing", synthetic_status="data gap exercise")
    assert watchdog.evaluate(gap).severity == SEVERITY_WARNING


def test_watchdog_detects_trade_stall_while_depth_flows() -> None:
    """Depth growing while trades stay flat for three checks raises a warning."""
    watchdog = Watchdog()
    base = dataclasses.replace(
        empty_prototype_dashboard_snapshot(),
        runtime_state="playing",
        synthetic_status="prototype stream active",
        trade_events=5,
    )

    reports = [
        watchdog.evaluate(dataclasses.replace(base, depth_events=depth))
        for depth in (10, 20, 30, 40)
    ]

    assert [report.severity for report in reports[:3]] == [SEVERITY_OK, SEVERITY_OK, SEVERITY_OK]
    assert reports[3].severity == SEVERITY_WARNING
    assert "stalled" in reports[3].message


def test_session_review_contains_totals_blocker_and_disclaimer(tmp_path: Path) -> None:
    """The review report counts decisions, names the chronic blocker, and is saved."""
    records = (
        _record("ACCEPTED", ACCEPTED_LINES, at="14:30:00"),
        _record("REJECTED", REJECTED_LINES, at="14:32:00"),
        _record("REJECTED", REJECTED_LINES, at="14:35:00"),
    )
    moment = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc)

    markdown = build_review(records, now=moment)
    path = write_review(records, tmp_path, now=moment)

    assert AI_COMMENTARY_LABEL in markdown
    assert "SYNTHETIC" in markdown
    assert "Decisions recorded: 3" in markdown
    assert "Accepted: 1" in markdown
    assert "## Chronic blocker" in markdown
    assert "**Level reclaim** (2 of 3 evaluations)" in markdown
    assert "Questions to verify by eye" in markdown
    assert path == tmp_path / "2026-07-12" / "ai_review_150000.md"
    assert path.read_text(encoding="utf-8") == markdown


def test_session_review_with_no_records_says_so() -> None:
    """An empty session produces a readable report, not an error."""
    markdown = build_review((), now=datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc))

    assert "No decisions were recorded" in markdown


def test_ask_answers_rejection_counters_and_fallback() -> None:
    """The ask agent narrates rejections, reads counters, and refuses to guess."""
    snapshot = dataclasses.replace(
        empty_prototype_dashboard_snapshot(),
        depth_events=120,
        trade_events=34,
        control_events=6,
    )
    history = (_record("ACCEPTED", ACCEPTED_LINES), _record("REJECTED", REJECTED_LINES))

    rejected_answer = answer_question("why was the setup rejected?", snapshot=snapshot, history=history)
    assert "nobody reloaded the bid" in rejected_answer

    counter_answer = answer_question("how many events so far?", snapshot=snapshot, history=history)
    assert "120 depth" in counter_answer
    assert "34 trade" in counter_answer

    fallback = answer_question("what is the meaning of life?", snapshot=snapshot, history=history)
    assert "not in the recorded state" in fallback


def test_ask_without_history_explains_the_gap() -> None:
    """Questions about decisions before any exist get a clear answer."""
    snapshot = empty_prototype_dashboard_snapshot()

    answer = answer_question("why rejected?", snapshot=snapshot, history=())

    assert "No rejected decisions" in answer


def test_narrate_record_round_trip() -> None:
    """Records narrate identically to their raw components."""
    record = _record("REJECTED", REJECTED_LINES)

    assert narrate_record(record) == narrate_decision(record.setup_name, record.decision, record.explanations)


def test_llm_backend_degrades_to_none_without_a_server() -> None:
    """With no local Ollama server, the optional backend fails soft."""
    unreachable = "http://127.0.0.1:9"

    assert ollama_available(unreachable, timeout=0.2) is False
    assert enhance_text("rewrite this", host=unreachable, timeout=0.2) is None


def test_agents_never_reference_execution_modules() -> None:
    """No agent module may import or mention the broker execution package."""
    agents_dir = Path(__file__).resolve().parents[1] / "app" / "agents"
    for module in agents_dir.glob("*.py"):
        assert "app.execution" not in module.read_text(encoding="utf-8"), module.name
