"""Versioned prop-firm rule profile loader - unresolved rules BLOCK execution.

The Lucid Trading rules must be verified from official Lucid sources before they
may govern automated execution. Until every field is verified (source URL +
retrieval date + resolved: true), :meth:`PropRuleProfile.blocks_automated_execution`
is True and the LIVE gate's ``prop_rules_resolved`` requirement fails. Guessing
rules is prohibited; an unresolved profile is the honest state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROFILE_PATH = Path("config/prop_rules_lucid.yaml")


REQUIRED_FIELDS = (
    "account_type", "account_size", "drawdown_method", "daily_loss_rule",
    "max_contracts", "consistency_rule", "permitted_instruments",
    "permitted_trading_times", "news_restrictions", "overnight_rules",
    "prohibited", "effective_date",
)


@dataclass(frozen=True, slots=True)
class PropRuleProfile:
    """One versioned prop-rule profile with provenance."""

    profile_version: str
    firm: str
    source_url: str
    retrieval_date: str
    account_type: str
    account_size: str
    drawdown_method: str
    daily_loss_rule: str
    max_contracts: str
    consistency_rule: str
    permitted_instruments: str
    permitted_trading_times: str
    news_restrictions: str
    overnight_rules: str
    prohibited: str
    effective_date: str
    resolved: bool
    unresolved_fields: tuple[str, ...]

    @property
    def blocks_automated_execution(self) -> bool:
        """Unverified rules always block automated (demo-armed or live) execution."""
        return not self.resolved or bool(self.unresolved_fields)

    @classmethod
    def load(cls, path: Path = DEFAULT_PROFILE_PATH) -> "PropRuleProfile":
        """Load the profile; a missing/unreadable file is fully unresolved."""
        if not path.is_file():
            return cls._unresolved("profile file missing")
        try:
            import yaml

            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - unreadable must fail closed
            return cls._unresolved("profile file unreadable")
        if not isinstance(payload, dict):
            return cls._unresolved("profile file malformed")
        fields = {name: payload.get(name) for name in REQUIRED_FIELDS}
        unresolved = tuple(sorted(
            name for name, value in fields.items()
            if value in (None, "", "unresolved") or str(value).startswith("UNRESOLVED")
        ))
        source_url = str(payload.get("source_url") or "")
        retrieval_date = str(payload.get("retrieval_date") or "")
        resolved = bool(payload.get("resolved") is True and source_url and retrieval_date and not unresolved)
        text = {name: str(fields[name] or "UNRESOLVED") for name in REQUIRED_FIELDS}
        return cls(
            profile_version=str(payload.get("profile_version", "0")),
            firm=str(payload.get("firm", "Lucid Trading")),
            source_url=source_url,
            retrieval_date=retrieval_date,
            account_type=text["account_type"],
            account_size=text["account_size"],
            drawdown_method=text["drawdown_method"],
            daily_loss_rule=text["daily_loss_rule"],
            max_contracts=text["max_contracts"],
            consistency_rule=text["consistency_rule"],
            permitted_instruments=text["permitted_instruments"],
            permitted_trading_times=text["permitted_trading_times"],
            news_restrictions=text["news_restrictions"],
            overnight_rules=text["overnight_rules"],
            prohibited=text["prohibited"],
            effective_date=text["effective_date"],
            resolved=resolved,
            unresolved_fields=unresolved,
        )

    @classmethod
    def _unresolved(cls, reason: str) -> "PropRuleProfile":
        text = {name: "UNRESOLVED" for name in REQUIRED_FIELDS}
        return cls(
            profile_version="0", firm="Lucid Trading", source_url="", retrieval_date="",
            resolved=False, unresolved_fields=(reason,), **text,
        )


def list_profiles(config_dir: Path = Path("config")) -> tuple[Path, ...]:
    """Return every prop-rule profile yaml the user can select or import.

    Looks for ``prop_rules_*.yaml`` in the config dir plus anything the user
    drops into ``config/prop_profiles/`` - a verified profile can be added and
    selected without any code change.
    """
    candidates: list[Path] = sorted(config_dir.glob("prop_rules_*.yaml"))
    profile_dir = config_dir / "prop_profiles"
    if profile_dir.is_dir():
        candidates.extend(sorted(profile_dir.glob("*.yaml")))
    return tuple(candidates)
