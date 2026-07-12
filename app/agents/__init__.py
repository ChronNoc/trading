"""Offline, rule-based assistant agents for narration, review, and monitoring.

Every agent in this package is deterministic and runs fully offline. An
optional local-LLM enhancement hook exists in :mod:`app.agents.llm_backend`,
but no agent requires it, and no agent ever talks to a broker or places
orders. All agent output is commentary about decisions the deterministic
strategy and risk engines already made.
"""

AI_COMMENTARY_LABEL = "AI COMMENTARY - NOT A TRADING SIGNAL"
