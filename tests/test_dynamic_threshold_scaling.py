"""A threshold written as a fraction of a past-only scale follows volatility."""

from __future__ import annotations

from decimal import Decimal

from app.strategy.dynamic_thresholds import DynamicThresholdEngine, RollingBaseline

ENGINE = DynamicThresholdEngine(conservative_fallbacks={"max_pullback_ticks": Decimal("6")})
RULE = {"kind": "reference_multiple", "multiple": "0.13"}


def test_the_threshold_follows_the_reference_instead_of_staying_fixed() -> None:
    calm = ENGINE.evaluate(
        "max_pullback_ticks", Decimal("5"), RULE, None, pass_when="lte", scale_reference=Decimal("40"),
    )
    wild = ENGINE.evaluate(
        "max_pullback_ticks", Decimal("5"), RULE, None, pass_when="lte", scale_reference=Decimal("400"),
    )

    assert calm.threshold_used == Decimal("5.20") and calm.passed and not calm.provisional
    assert wild.threshold_used == Decimal("52.00") and wild.passed
    # The same raw value passes a loose calm-market limit only because the limit scaled.
    tight = ENGINE.evaluate(
        "max_pullback_ticks", Decimal("6"), RULE, None, pass_when="lte", scale_reference=Decimal("40"),
    )
    assert tight.passed is False


def test_without_a_usable_reference_it_falls_back_to_the_conservative_value() -> None:
    for reference in (None, Decimal("0"), Decimal("-5")):
        result = ENGINE.evaluate(
            "max_pullback_ticks", Decimal("5"), RULE, None, pass_when="lte", scale_reference=reference,
        )
        assert result.threshold_used == Decimal("6")
        assert result.provisional is True
        assert "no scale reference" in result.reason


def test_existing_rule_kinds_ignore_the_reference_and_are_unchanged() -> None:
    baseline = RollingBaseline.from_values("x", [Decimal(value) for value in range(1, 101)])
    percentile = {"kind": "percentile", "percentile": "90"}

    with_reference = ENGINE.evaluate("x", Decimal("50"), percentile, baseline, scale_reference=Decimal("1000"))
    without = ENGINE.evaluate("x", Decimal("50"), percentile, baseline)

    assert with_reference == without
    assert with_reference.threshold_used == Decimal("90")
    # no rule and no baseline: still the conservative fallback with its old reason
    bare = ENGINE.evaluate("max_pullback_ticks", Decimal("5"), None, None, scale_reference=Decimal("40"))
    assert bare.threshold_used == Decimal("6") and "no baseline data" in bare.reason
