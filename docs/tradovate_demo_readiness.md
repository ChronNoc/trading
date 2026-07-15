# Tradovate DEMO readiness (STAGE 8) — read-only assessment

**Status: LOCKED. No order is submitted by any code path in this build.**
This document is the result of a *read-only* inspection. It does not enable demo
or live trading and does not modify any execution or risk file.

## What already exists (inspected, not modified)

The repository already contains a **demo-only** execution layer. None of it is
wired into the automatic runtime or the GUI, and the pipeline's Tradovate DEMO
gate is locked by construction (`app/research/pipeline_status.py`, stage 8).

| Module | Role | Notes |
|---|---|---|
| `app/execution/orders.py` | Demo Tradovate order primitives (`submit_order`, `cancel_order`, `flatten_position`, ack polling) | Every mutating call requires a `RiskApproval`; targets the **demo** base URL only. |
| `app/execution/brackets.py` | Demo bracket (entry + stop + target) orchestration | Builds payloads; no live endpoint. |
| `app/execution/dry_run.py` | `DryRunExecutor` — logs intended orders, *can never send one* | The safe default path. |
| `app/execution/reconciliation.py` | Position/or­der reconciliation | **Protected file** — not touched. |
| `app/risk/limits.py`, `sizing.py`, `kill_switch.py` | Risk engine | **Protected files** — not touched. |

## What remains before a DEMO order could ever be placed

1. **Explicit config gate.** A `demo_execution_enabled` flag defaulting to
   `false`, never set by generated code, separate from `OBSERVE`/`LIVE` mode.
2. **Validation gate must pass first.** The profitability evidence ladder
   (`app/research/profitability_progress.py`) must reach
   `profitable_claim_supported = True` on out-of-sample real data. Today it is at
   the data-collection stage (0 completed setups), so this is far off.
3. **Credential handling.** Tradovate demo credentials must be supplied at
   runtime by the user and **never stored in the repo**. Not implemented here by
   design.
4. **Runtime wiring.** Connect the canonical paper account's accepted setups to
   `DryRunExecutor` first, then — only behind the flag and the validation gate —
   to the demo `submit_order`/`place_bracket_order` path, always through the
   protected risk engine (`RiskApproval`).
5. **Lucid Trading rule encoding.** The current prop-style limits in
   `RealPaperLedgerConfig` (3 trades/day, 3 losses/day, 1% daily risk, no reset)
   are placeholders. The **official current Lucid Trading rules must be verified
   from Lucid's own materials** before being encoded, in a separate, explicitly
   authorized task.
6. **Tests.** Demo-order integration tests against a mocked Tradovate demo
   endpoint; a test proving LIVE stays unreachable; a test proving no order is
   submitted unless both the flag and the validation gate are satisfied.

## LIVE

LIVE execution is **unavailable** in this application (pipeline stage 9, locked).
It is explicitly out of scope and requires a separate reviewed safety gate beyond
the demo gate.

---

## Future-task prompt (for a later, explicitly authorized session)

> Implement Tradovate **DEMO** execution for the MNQ assistant. Preconditions,
> all mandatory:
> - The profitability validation gate
>   (`app/research/profitability_progress.py`) reports
>   `profitable_claim_supported = True` on out-of-sample real data with the
>   configured minimum completed-setup sample and independent-day count.
> - A new `demo_execution_enabled` config flag (default `false`) is explicitly
>   turned on by the user; generated code must never set it.
> Scope:
> - Wire the canonical paper account's accepted setups to `DryRunExecutor`
>   first; add a demo path via `app/execution/orders.py` +
>   `app/execution/brackets.py`, every mutating call gated by a `RiskApproval`
>   from the protected risk engine.
> - Verify the **current** Lucid Trading prop rules from Lucid's official
>   materials and encode them as the demo account limits, with tests.
> - Credentials are provided at runtime and never stored.
> - Add tests: demo submit/cancel/flatten against a mocked demo endpoint;
>   ack-timeout handling; proof LIVE remains unreachable; proof no order is
>   submitted unless both the flag and the validation gate pass.
> - Keep LIVE unavailable. Do not modify `risk/limits.py`, `risk/sizing.py`,
>   `risk/kill_switch.py`, `execution/reconciliation.py`, or
>   `config/production_config.yaml` without explicit instruction.
