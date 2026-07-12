"""Strategy-discovery pipeline: walk-forward search over parameter variants.

Everything in this package operates on simulated/replay data and ends at
the validation gate: a recommendation report for human review. Nothing
here can flip OBSERVE to LIVE, open a broker connection, or place an
order - that boundary is enforced by :mod:`app.discovery.supervisor` and
verified by tests.
"""
