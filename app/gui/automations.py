"""Rule-based GUI automation engine for the prototype dashboard.

Twelve deterministic automations that react to dashboard snapshots each
refresh tick. They automate the workflow around the trader - pausing on
feed gaps, journaling, exporting, coaching - and never touch strategy,
risk, or execution logic. Every automation can be toggled off in the GUI.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.agents.records import DecisionRecord, canonical_condition_name, parse_explanation_lines
from app.agents.session_reviewer import write_review
from app.agents.watchdog import SEVERITY_WARNING, WatchdogReport
from app.prototype.scenarios import PrototypeDashboardSnapshot

_FLASH_TICKS = 5
_SLOW_TICKS = 8
_REPLAY_REFRESH_EVERY = 10
_ENTRY_LIMIT = 3


@dataclass(frozen=True, slots=True)
class AutomationDefinition:
    """One automation with its GUI-facing name and description."""

    key: str
    title: str
    description: str


AUTOMATION_DEFINITIONS: tuple[AutomationDefinition, ...] = (
    AutomationDefinition(
        "auto_pause_on_gap",
        "Pause on feed gap",
        "Pauses playback during a disconnect or data gap and resumes when the feed recovers.",
    ),
    AutomationDefinition(
        "auto_slow_on_decision",
        "Slow down on decisions",
        "Drops playback speed when a setup decision lands so you can watch it, then restores speed.",
    ),
    AutomationDefinition(
        "auto_flash_on_accept",
        "Flash accepted setups",
        "Shows a header banner for a few seconds whenever a setup is ACCEPTED (shadow only).",
    ),
    AutomationDefinition(
        "auto_export_decisions_csv",
        "Export decisions to CSV",
        "Appends every decision to automation/decisions_log.csv for later analysis.",
    ),
    AutomationDefinition(
        "auto_snapshot_on_decision",
        "Snapshot state on decision",
        "Saves a JSON snapshot of the dashboard state for every decision.",
    ),
    AutomationDefinition(
        "auto_review_on_complete",
        "Auto-review at session end",
        "Writes the markdown session review automatically when the scenario completes.",
    ),
    AutomationDefinition(
        "auto_entry_limit_guard",
        "Daily entry-limit guard",
        "Shows a persistent warning once 3 setups are accepted - the live risk lock would refuse more.",
    ),
    AutomationDefinition(
        "auto_blocker_coach",
        "Chronic-blocker coach",
        "Names the condition that keeps failing across recent rejections so you review that threshold.",
    ),
    AutomationDefinition(
        "auto_watchdog_journal",
        "Watchdog journal",
        "Appends every watchdog warning to automation/watchdog_events.jsonl.",
    ),
    AutomationDefinition(
        "auto_latest_report_pointer",
        "Latest-report pointer",
        "Keeps automation/LATEST_REPORT.txt pointing at the newest session report.",
    ),
    AutomationDefinition(
        "auto_refresh_replay",
        "Auto-refresh replay list",
        "Rescans recorded sessions periodically so new recordings appear without restarting.",
    ),
    AutomationDefinition(
        "auto_startup_checklist",
        "Startup checklist",
        "Checks the environment (venv, scenarios, writable data, last self-check) at startup.",
    ),
)


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    """One startup-checklist result."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class AutomationActions:
    """Directives for the GUI produced by one engine tick."""

    control_commands: tuple[str, ...] = ()
    flash: str | None = None
    persistent_banner: str | None = None
    coach: str | None = None
    refresh_replay: bool = False


class AutomationEngine:
    """Stateful engine fed one dashboard snapshot per GUI refresh tick."""

    def __init__(self, *, output_root: Path, repo_root: Path | None = None) -> None:
        """Create the engine writing automation files under ``output_root``."""
        self.output_root = Path(output_root)
        self.repo_root = Path(repo_root) if repo_root is not None else Path(".")
        self.enabled: dict[str, bool] = {item.key: True for item in AUTOMATION_DEFINITIONS}
        self.fire_counts: Counter[str] = Counter()
        self._tick = 0
        self._seen_decisions = 0
        self._paused_for_gap = False
        self._slow_restore_speed: int | None = None
        self._slow_ticks_left = 0
        self._flash_message = ""
        self._flash_ticks_left = 0
        self._review_written = False
        self._last_report_pointer = ""
        self._last_journal_message = ""

    @property
    def automation_dir(self) -> Path:
        """Directory holding all automation output files."""
        return self.output_root / "automation"

    def process(
        self,
        snapshot: PrototypeDashboardSnapshot,
        history: tuple[DecisionRecord, ...],
        watchdog_report: WatchdogReport | None = None,
    ) -> AutomationActions:
        """Run every enabled automation against the current state."""
        self._tick += 1
        commands: list[str] = []
        new_records = history[self._seen_decisions:]
        self._seen_decisions = len(history)

        self._automate_gap_pause(snapshot, commands)
        self._automate_slow_on_decision(snapshot, new_records, commands)
        self._automate_flash(new_records)
        self._automate_csv_export(new_records)
        self._automate_decision_snapshots(snapshot, new_records)
        review_flash = self._automate_review_on_complete(snapshot, history)
        persistent = self._automate_entry_limit(history)
        coach = self._automate_blocker_coach(history)
        self._automate_watchdog_journal(watchdog_report)
        self._automate_report_pointer(snapshot)
        refresh_replay = self._automate_replay_refresh()

        flash = review_flash or self._current_flash()
        return AutomationActions(
            control_commands=tuple(commands),
            flash=flash,
            persistent_banner=persistent,
            coach=coach,
            refresh_replay=refresh_replay,
        )

    def startup_checklist(self) -> tuple[ChecklistItem, ...]:
        """Check the environment once at startup."""
        if not self.enabled["auto_startup_checklist"]:
            return ()
        self.fire_counts["auto_startup_checklist"] += 1
        items: list[ChecklistItem] = []

        venv_python = self.repo_root / ".venv" / "Scripts" / "python.exe"
        items.append(
            ChecklistItem(
                "Virtual environment",
                venv_python.is_file(),
                str(venv_python) if venv_python.is_file() else "missing .venv - create it and install dependencies",
            ),
        )

        scenario_dir = self.repo_root / "config" / "prototype_scenarios"
        scenario_count = len(list(scenario_dir.glob("*.yaml"))) if scenario_dir.is_dir() else 0
        items.append(
            ChecklistItem(
                "Scenario library",
                scenario_count > 0,
                f"{scenario_count} scenario file(s)" if scenario_count else "no scenario files found",
            ),
        )

        try:
            self.automation_dir.mkdir(parents=True, exist_ok=True)
            probe = self.automation_dir / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            items.append(ChecklistItem("Data directory writable", True, str(self.automation_dir)))
        except OSError as error:
            items.append(ChecklistItem("Data directory writable", False, str(error)))

        report_dir = self.repo_root / "logs" / "self_check"
        reports = sorted(report_dir.glob("self_check_*.md")) if report_dir.is_dir() else []
        if reports:
            latest = reports[-1]
            passed = latest.read_text(encoding="utf-8").startswith("# Self-check PASS")
            items.append(
                ChecklistItem(
                    "Last self-check",
                    passed,
                    latest.name if passed else f"{latest.name} FAILED - fix the suite before trusting the prototype",
                ),
            )
        else:
            items.append(
                ChecklistItem("Last self-check", False, "never run - run run_self_check.bat"),
            )
        return tuple(items)

    def _automate_gap_pause(self, snapshot: PrototypeDashboardSnapshot, commands: list[str]) -> None:
        if not self.enabled["auto_pause_on_gap"]:
            return
        in_gap = snapshot.synthetic_status in ("reconnecting", "data gap exercise")
        if in_gap and not snapshot.paused and not self._paused_for_gap:
            commands.append("pause")
            self._paused_for_gap = True
            self.fire_counts["auto_pause_on_gap"] += 1
        elif not in_gap and self._paused_for_gap and snapshot.paused:
            commands.append("resume")
            self._paused_for_gap = False

    def _automate_slow_on_decision(
        self,
        snapshot: PrototypeDashboardSnapshot,
        new_records: tuple[DecisionRecord, ...],
        commands: list[str],
    ) -> None:
        if not self.enabled["auto_slow_on_decision"]:
            return
        if new_records and snapshot.playback_speed > 2 and self._slow_restore_speed is None:
            self._slow_restore_speed = snapshot.playback_speed
            self._slow_ticks_left = _SLOW_TICKS
            commands.append("speed:2")
            self.fire_counts["auto_slow_on_decision"] += 1
            return
        if self._slow_restore_speed is not None:
            self._slow_ticks_left -= 1
            if self._slow_ticks_left <= 0:
                commands.append(f"speed:{self._slow_restore_speed}")
                self._slow_restore_speed = None

    def _automate_flash(self, new_records: tuple[DecisionRecord, ...]) -> None:
        if not self.enabled["auto_flash_on_accept"]:
            return
        for record in new_records:
            if record.accepted:
                self._flash_message = (
                    f"SETUP ACCEPTED at {record.recorded_at} - shadow bracket only, no real order exists."
                )
                self._flash_ticks_left = _FLASH_TICKS
                self.fire_counts["auto_flash_on_accept"] += 1

    def _current_flash(self) -> str | None:
        if self._flash_ticks_left <= 0:
            return None
        self._flash_ticks_left -= 1
        return self._flash_message

    def _automate_csv_export(self, new_records: tuple[DecisionRecord, ...]) -> None:
        if not self.enabled["auto_export_decisions_csv"] or not new_records:
            return
        path = self.automation_dir / "decisions_log.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("recorded_at,setup,decision,failed_conditions,explanations\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            for record in new_records:
                failed = sum(
                    1 for condition in parse_explanation_lines(record.explanations) if not condition.passed
                )
                explanations = " | ".join(record.explanations).replace(",", ";")
                handle.write(
                    f"{record.recorded_at},{record.setup_name},{record.decision},{failed},{explanations}\n",
                )
                self.fire_counts["auto_export_decisions_csv"] += 1

    def _automate_decision_snapshots(
        self,
        snapshot: PrototypeDashboardSnapshot,
        new_records: tuple[DecisionRecord, ...],
    ) -> None:
        if not self.enabled["auto_snapshot_on_decision"] or not new_records:
            return
        directory = self.automation_dir / "decision_snapshots"
        directory.mkdir(parents=True, exist_ok=True)
        for record in new_records:
            index = self.fire_counts["auto_snapshot_on_decision"] + 1
            payload = {
                "recorded_at": record.recorded_at,
                "setup_name": record.setup_name,
                "decision": record.decision,
                "explanations": list(record.explanations),
                "session": snapshot.session,
                "regime": snapshot.regime,
                "depth_events": snapshot.depth_events,
                "trade_events": snapshot.trade_events,
                "last_trade_price": snapshot.last_trade_price,
                "synthetic": True,
            }
            (directory / f"decision_{index:03d}.json").write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
            self.fire_counts["auto_snapshot_on_decision"] += 1

    def _automate_review_on_complete(
        self,
        snapshot: PrototypeDashboardSnapshot,
        history: tuple[DecisionRecord, ...],
    ) -> str | None:
        if not self.enabled["auto_review_on_complete"] or self._review_written:
            return None
        if snapshot.synthetic_status != "complete" or not history:
            return None
        path = write_review(history, self.automation_dir / "session_reviews")
        self._review_written = True
        self.fire_counts["auto_review_on_complete"] += 1
        return f"Session review auto-generated: {path}"

    def _automate_entry_limit(self, history: tuple[DecisionRecord, ...]) -> str | None:
        if not self.enabled["auto_entry_limit_guard"]:
            return None
        accepted = sum(1 for record in history if record.accepted)
        if accepted < _ENTRY_LIMIT:
            return None
        self.fire_counts["auto_entry_limit_guard"] += 1
        return (
            f"Daily entry limit reached ({accepted}/{_ENTRY_LIMIT} accepted) - "
            "in live trading the risk lock would refuse every further entry today."
        )

    def _automate_blocker_coach(self, history: tuple[DecisionRecord, ...]) -> str | None:
        if not self.enabled["auto_blocker_coach"]:
            return None
        rejected = [record for record in history if not record.accepted][-3:]
        if len(rejected) < 2:
            return None
        failure_sets = [
            {
                canonical_condition_name(condition.message)
                for condition in parse_explanation_lines(record.explanations)
                if not condition.passed
            }
            for record in rejected
        ]
        common = set.intersection(*failure_sets) if failure_sets else set()
        if not common:
            return None
        blocker = sorted(common)[0]
        self.fire_counts["auto_blocker_coach"] += 1
        return (
            f"Chronic blocker: '{blocker}' failed in your last {len(rejected)} rejections - "
            "review that threshold before trusting more signals."
        )

    def _automate_watchdog_journal(self, report: WatchdogReport | None) -> None:
        if not self.enabled["auto_watchdog_journal"] or report is None:
            return
        if report.severity != SEVERITY_WARNING or report.message == self._last_journal_message:
            return
        self.automation_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "severity": report.severity,
            "message": report.message,
        }
        with (self.automation_dir / "watchdog_events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        self._last_journal_message = report.message
        self.fire_counts["auto_watchdog_journal"] += 1

    def _automate_report_pointer(self, snapshot: PrototypeDashboardSnapshot) -> None:
        if not self.enabled["auto_latest_report_pointer"]:
            return
        report_path = snapshot.report_path
        if report_path in ("", "not written yet") or report_path == self._last_report_pointer:
            return
        self.automation_dir.mkdir(parents=True, exist_ok=True)
        (self.automation_dir / "LATEST_REPORT.txt").write_text(report_path + "\n", encoding="utf-8")
        self._last_report_pointer = report_path
        self.fire_counts["auto_latest_report_pointer"] += 1

    def _automate_replay_refresh(self) -> bool:
        if not self.enabled["auto_refresh_replay"]:
            return False
        if self._tick % _REPLAY_REFRESH_EVERY != 0:
            return False
        self.fire_counts["auto_refresh_replay"] += 1
        return True
