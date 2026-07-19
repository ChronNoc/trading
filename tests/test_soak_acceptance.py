"""Regression tests for truthful long-soak verdicts."""

from tools.soak import _record_shutdown_result


def test_unclean_shutdown_can_never_report_a_passing_soak() -> None:
    """Exact event conservation is insufficient when terminal drain failed."""
    failures: list[str] = []

    _record_shutdown_result(failures, drained_cleanly=False)

    assert failures == ["receiver shutdown did not drain cleanly within 20 seconds"]


def test_clean_shutdown_adds_no_failure() -> None:
    """A confirmed clean drain does not alter an otherwise healthy verdict."""
    failures: list[str] = []

    _record_shutdown_result(failures, drained_cleanly=True)

    assert failures == []
