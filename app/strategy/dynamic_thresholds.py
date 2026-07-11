"""Dynamic threshold calibration from past-only baseline statistics."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True, slots=True)
class ThresholdResult:
    """Explainable dynamic threshold evaluation."""

    feature_name: str
    raw_value: Decimal
    normalized_value: Decimal | None
    threshold_used: Decimal
    passed: bool
    reason: str
    provisional: bool


@dataclass(slots=True)
class RollingBaseline:
    """Past-only rolling baseline for one feature."""

    feature_name: str
    values: list[Decimal] = field(default_factory=list)

    @classmethod
    def from_values(cls, feature_name: str, values: Iterable[Decimal]) -> "RollingBaseline":
        """Create a baseline from historical values."""
        return cls(feature_name=feature_name, values=list(values))

    def percentile(self, percentile: Decimal) -> Decimal | None:
        """Return a nearest-rank percentile from already-observed values."""
        if not self.values:
            return None
        sorted_values = sorted(self.values)
        percentile = min(max(percentile, Decimal("0")), Decimal("100"))
        index = int(((percentile / Decimal("100")) * Decimal(len(sorted_values) - 1)).to_integral_value())
        return sorted_values[index]

    def median(self) -> Decimal | None:
        """Return the median from already-observed values."""
        if not self.values:
            return None
        sorted_values = sorted(self.values)
        middle = len(sorted_values) // 2
        if len(sorted_values) % 2:
            return sorted_values[middle]
        return (sorted_values[middle - 1] + sorted_values[middle]) / Decimal("2")

    def append(self, value: Decimal) -> None:
        """Append a newly observed value after threshold evaluation."""
        self.values.append(value)


@dataclass(frozen=True, slots=True)
class DynamicThresholdEngine:
    """Evaluate feature thresholds without changing hard risk rules."""

    conservative_fallbacks: dict[str, Decimal]

    def evaluate(
        self,
        feature_name: str,
        raw_value: Decimal,
        rule: dict[str, object] | None,
        baseline: RollingBaseline | None,
        *,
        pass_when: str = "gte",
    ) -> ThresholdResult:
        """Evaluate one dynamic threshold and include raw/normalized/reason fields."""
        threshold, provisional, reason = self._threshold(feature_name, rule, baseline)
        normalized = _normalize(raw_value, baseline)
        passed = raw_value >= threshold if pass_when == "gte" else raw_value <= threshold
        return ThresholdResult(
            feature_name=feature_name,
            raw_value=raw_value,
            normalized_value=normalized,
            threshold_used=threshold,
            passed=passed,
            reason=reason,
            provisional=provisional,
        )

    def _threshold(
        self,
        feature_name: str,
        rule: dict[str, object] | None,
        baseline: RollingBaseline | None,
    ) -> tuple[Decimal, bool, str]:
        fallback = self.conservative_fallbacks.get(feature_name, Decimal("0"))
        if baseline is None or not baseline.values:
            return fallback, True, "using conservative fallback; no baseline data available"

        if not rule:
            return fallback, True, "using conservative fallback; no threshold rule configured"

        kind = str(rule.get("kind", "fallback"))
        if kind == "percentile":
            percentile_value = Decimal(str(rule.get("percentile", "50")))
            threshold = baseline.percentile(percentile_value)
            if threshold is not None:
                return threshold, False, f"using historical {percentile_value}th percentile"
        if kind == "median_multiple":
            multiple = Decimal(str(rule.get("multiple", "1")))
            median = baseline.median()
            if median is not None:
                return median * multiple, False, f"using historical median x {multiple}"

        return fallback, True, "using conservative fallback; threshold rule could not be evaluated"


def _normalize(raw_value: Decimal, baseline: RollingBaseline | None) -> Decimal | None:
    if baseline is None or not baseline.values:
        return None
    median = baseline.median()
    if median in {None, Decimal("0")}:
        return None
    return raw_value / median

