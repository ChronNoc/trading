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


@dataclass(frozen=True, slots=True)
class PropRuleProfile:
    """One versioned prop-rule profile with provenance."""

    profile_version: str
    firm: str
    source_url: str
    retrieval_date: str
    account_type: str
    drawdown_method: str
    daily_loss_rule: str
    max_contracts: str
    consistency_rule: str
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
        fields = {
            "account_type": payload.get("account_type"),
            "drawdown_method": payload.get("drawdown_method"),
            "daily_loss_rule": payload.get("daily_loss_rule"),
            "max_contracts": payload.get("max_contracts"),
            "consistency_rule": payload.get("consistency_rule"),
            "prohibited": payload.get("prohibited"),
            "effective_date": payload.get("effective_date"),
        }
        unresolved = tuple(sorted(
            name for name, value in fields.items()
            if value in (None, "", "unresolved") or str(value).startswith("UNRESOLVED")
        ))
        source_url = str(payload.get("source_url") or "")
        retrieval_date = str(payload.get("retrieval_date") or "")
        resolved = bool(payload.get("resolved") is True and source_url and retrieval_date and not unresolved)
        return cls(
            profile_version=str(payload.get("profile_version", "0")),
            firm=str(payload.get("firm", "Lucid Trading")),
            source_url=source_url,
            retrieval_date=retrieval_date,
            account_type=str(fields["account_type"] or "UNRESOLVED"),
            drawdown_method=str(fields["drawdown_method"] or "UNRESOLVED"),
            daily_loss_rule=str(fields["daily_loss_rule"] or "UNRESOLVED"),
            max_contracts=str(fields["max_contracts"] or "UNRESOLVED"),
            consistency_rule=str(fields["consistency_rule"] or "UNRESOLVED"),
            prohibited=str(fields["prohibited"] or "UNRESOLVED"),
            effective_date=str(fields["effective_date"] or "UNRESOLVED"),
            resolved=resolved,
            unresolved_fields=unresolved,
        )

    @classmethod
    def _unresolved(cls, reason: str) -> "PropRuleProfile":
        return cls(
            profile_version="0", firm="Lucid Trading", source_url="", retrieval_date="",
            account_type="UNRESOLVED", drawdown_method="UNRESOLVED", daily_loss_rule="UNRESOLVED",
            max_contracts="UNRESOLVED", consistency_rule="UNRESOLVED", prohibited="UNRESOLVED",
            effective_date="UNRESOLVED", resolved=False,
            unresolved_fields=(reason,),
        )
