"""Formal strategy specification models and YAML loading helpers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias, cast

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, StringConstraints, computed_field

UNRESOLVED: Literal["unresolved"] = "unresolved"
ParameterValue: TypeAlias = Decimal | Literal["unresolved"]
StrategyTime: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$"),
]


class ImportantLevel(str, Enum):
    """Supported important level names for context filtering."""

    PRIOR_DAY_HIGH = "prior_day_high"
    PRIOR_DAY_LOW = "prior_day_low"
    OVERNIGHT_HIGH = "overnight_high"
    OVERNIGHT_LOW = "overnight_low"
    MANUALLY_DEFINED_LEVEL = "manually_defined_level"


class StrictSpecModel(BaseModel):
    """Base model settings shared by strategy specification sections."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _to_decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be numeric, not bool")

    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be a finite decimal number") from error

    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal number")

    return decimal_value


def _coerce_parameter_value(value: object) -> ParameterValue:
    if value == UNRESOLVED:
        return UNRESOLVED

    if isinstance(value, str):
        raise ValueError(f'parameter values must be numeric or "{UNRESOLVED}"')

    return _to_decimal(value, "parameter value")


def _coerce_decimal(value: object) -> Decimal:
    return _to_decimal(value, "decimal value")


ResolvedOrUnresolved: TypeAlias = Annotated[
    ParameterValue,
    BeforeValidator(_coerce_parameter_value),
]
DecimalValue: TypeAlias = Annotated[Decimal, BeforeValidator(_coerce_decimal)]


class InstrumentSpec(StrictSpecModel):
    """Instrument constraints for the strategy specification."""

    symbol: str
    allowed_contracts: int


class SessionSpec(StrictSpecModel):
    """Session window constraints for evaluating the strategy."""

    timezone: str
    allowed_start: StrategyTime
    allowed_end: StrategyTime


class ContextRequirementsSpec(StrictSpecModel):
    """Market context filters required before evaluating entry setup rules."""

    trend_condition: str
    important_levels: list[ImportantLevel]


class EntrySetupSpec(StrictSpecModel):
    """Entry setup thresholds, with unresolved values allowed during research."""

    liquidity_minimum: ResolvedOrUnresolved
    aggressive_volume_minimum: ResolvedOrUnresolved
    maximum_price_progress_ticks: ResolvedOrUnresolved
    reclaim_ticks: ResolvedOrUnresolved
    confirmation_window_ms: ResolvedOrUnresolved
    min_reload_count: ResolvedOrUnresolved = Decimal("1")
    min_ask_pull_ratio: ResolvedOrUnresolved = Decimal("0.50")


class RiskSpec(StrictSpecModel):
    """Risk constraints for evaluating whether a setup is allowed."""

    maximum_trades_per_day: int
    daily_loss_fraction: DecimalValue
    allow_averaging_down: bool = False
    maximum_open_positions: int


class ExitSpec(StrictSpecModel):
    """Exit behavior parameters, with unresolved values allowed during research."""

    stop_method: ResolvedOrUnresolved
    target_method: ResolvedOrUnresolved
    break_even_rule: ResolvedOrUnresolved
    time_stop_seconds: ResolvedOrUnresolved


class StrategySpec(StrictSpecModel):
    """Complete formal strategy specification."""

    instrument: InstrumentSpec
    session: SessionSpec
    context_requirements: ContextRequirementsSpec
    entry_setup: EntrySetupSpec
    risk: RiskSpec
    exit: ExitSpec

    @computed_field
    @property
    def unresolved_parameters(self) -> tuple[str, ...]:
        """Return dotted field paths whose value is the unresolved marker."""
        return tuple(_collect_unresolved_paths(self))

    def list_unresolved_parameters(self) -> tuple[str, ...]:
        """Return every unresolved parameter path in stable specification order."""
        return self.unresolved_parameters


def _collect_unresolved_paths(value: object, prefix: str = "") -> list[str]:
    if value == UNRESOLVED:
        return [prefix]

    if isinstance(value, BaseModel):
        paths: list[str] = []
        for field_name in value.__class__.model_fields:
            field_value = getattr(value, field_name)
            child_prefix = f"{prefix}.{field_name}" if prefix else field_name
            paths.extend(_collect_unresolved_paths(field_value, child_prefix))
        return paths

    if isinstance(value, (tuple, list)):
        paths = []
        for index, item in enumerate(value):
            child_prefix = f"{prefix}[{index}]"
            paths.extend(_collect_unresolved_paths(item, child_prefix))
        return paths

    return []


def parse_strategy_spec_yaml(yaml_text: str) -> StrategySpec:
    """Parse YAML text into a validated strategy specification."""
    raw_spec = yaml.safe_load(yaml_text)

    if not isinstance(raw_spec, dict):
        raise ValueError("strategy spec YAML must contain a mapping at the document root")

    return StrategySpec.model_validate(cast(dict[str, Any], raw_spec))


def load_strategy_spec(path: str | Path) -> StrategySpec:
    """Load and validate a strategy specification from a YAML file."""
    spec_path = Path(path)
    return parse_strategy_spec_yaml(spec_path.read_text(encoding="utf-8"))
