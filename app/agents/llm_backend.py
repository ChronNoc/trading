"""Optional free local-LLM text enhancement through an Ollama server.

Ollama (https://ollama.com) is a free, local LLM runtime. Nothing in this
project requires it: every agent produces deterministic text on its own.
When an Ollama server happens to be running on the default local port,
these helpers can rephrase agent commentary into richer prose. All
failures degrade to ``None`` so callers keep their deterministic output.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "llama3.2"


def ollama_available(host: str = DEFAULT_HOST, *, timeout: float = 0.5) -> bool:
    """Return true when a local Ollama server answers on the given host."""
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def enhance_text(
    prompt: str,
    *,
    host: str = DEFAULT_HOST,
    model: str = DEFAULT_MODEL,
    timeout: float = 10.0,
) -> str | None:
    """Ask a local Ollama model to rewrite commentary; return None on any failure.

    The caller must always hold a deterministic fallback. This function
    never raises and never blocks longer than the given timeout.
    """
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode("utf-8")
    request = urllib.request.Request(
        f"{host}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    text = str(body.get("response", "")).strip()
    return text or None
