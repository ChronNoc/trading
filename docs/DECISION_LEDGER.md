# Decision Ledger

## D-001 — Provenance and registry before runtime scoring
- **Date / owner:** 2026-07-23, Executive Product Council + Architecture + ML.
- **Problem:** Runtime scoring would be untrustworthy while training bypassed catalog eligibility and no immutable approved artifact existed.
- **Selected:** Build strict session gate, deterministic dataset, time-aware validation, immutable unapproved challenger, and GUI evidence first.
- **Rejected:** Add a quick “latest model” runtime loader.
- **Tradeoff:** Delays predictions one cycle; prevents silent data/model mismatch.
- **Revisit:** After offline/online feature parity is proven.

## D-002 — Protocol 1.2 minimum for model evidence
- **Evidence:** 1.2 introduces batching/global stream sequence required for continuity attribution.
- **Selected:** Older/incomplete sessions may remain descriptive but fail closed for ML.
- **Revisit:** Only with a versioned migration proving equivalent continuity.

## D-003 — Content-addressed immutable artifacts
- **Selected:** Dataset and challenger IDs derive from stable manifests/hashes; conflicting overwrite is refused; identical rerun is idempotent.
- **Rejected:** Anonymous files or mutable semantic-version paths as canonical identity.

## D-004 — Exact ID plus SHA approval
- **Selected:** Human approval must name an artifact ID and SHA. Directory order, mtime, and “latest” are invalid selectors.
- **Tradeoff:** More explicit operations; safe rollback and auditability.

## D-005 — Logistic regression first
- **Selected:** Deterministic `liblinear` baseline with fixed seed/hyperparameters.
- **Rejected:** Deep temporal/RL/online self-modification before simpler baselines and parity.
- **Revisit:** Only if robust OOS evidence and residual error justify complexity.

## D-006 — No automatic promotion or runtime loading in cycle one
- **Selected:** Registry status is challenger; approval defaults null; runtime flags false.
- **Reason:** Training, validation, promotion, and deployment are separate safety boundaries.

## D-007 — Declared and observed capability remain separate
- **Selected:** Manifest preserves handshake declarations and observed event coverage independently.
- **Reason:** A provider claim is not proof that data arrived; missing data is not zero.

## D-008 — Default supervisor owns backend
- **Selected:** Default GUI launch attaches to a persistent supervisor/backend; direct backend CLI remains intentionally unsupervised for tests/headless use.
- **Evidence:** `tools/start_assistant.py`, `tools/backend_supervisor.py`, `tests/test_process_isolation.py`.
