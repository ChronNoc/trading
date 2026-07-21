"""Send a phone push on each closed paper trade via a Telegram bot.

Opt-in and self-contained: the running backend owns it, so alerts arrive even
when no editor/agent is attached. It is OFF unless BOTH environment variables
are set (never committed, never logged):

    TELEGRAM_BOT_TOKEN   from @BotFather
    TELEGRAM_CHAT_ID     your chat id (message the bot, then read getUpdates)

Delivery is fire-and-forget on a daemon thread so a slow or failing network
call can never delay capture or the trade path, and a notifier error never
propagates into the engine. The token is never placed in message text or logs.
"""

from __future__ import annotations

import os
import threading
from typing import Callable

TradeSender = Callable[[str, str, str], None]

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ENV = "TELEGRAM_CHAT_ID"


class TelegramNotifier:
    """Format and deliver closed-trade alerts; a no-op unless configured."""

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        *,
        sender: TradeSender | None = None,
    ) -> None:
        """Read credentials from args or env; inject ``sender`` in tests."""
        self._token = token or os.environ.get(TOKEN_ENV)
        self._chat_id = chat_id or os.environ.get(CHAT_ENV)
        self._sender = sender or _http_send

    @property
    def enabled(self) -> bool:
        """True only when both a bot token and a chat id are present."""
        return bool(self._token and self._chat_id)

    def notify_trade(self, trade: object) -> None:
        """Fire-and-forget a trade alert; returns immediately, never raises."""
        if not self.enabled:
            return
        text = format_trade(trade)
        thread = threading.Thread(target=self._deliver, args=(text,),
                                  name="telegram-notify", daemon=True)
        thread.start()

    def _deliver(self, text: str) -> bool:
        """Synchronous send used by the thread and by tests; swallows errors."""
        assert self._token is not None and self._chat_id is not None
        try:
            self._sender(self._token, self._chat_id, text)
            return True
        except Exception:  # noqa: BLE001 - a notification must never crash the backend
            return False


def format_trade(trade: object) -> str:
    """Build a compact, non-sensitive alert line for one closed trade."""
    direction = getattr(getattr(trade, "direction", None), "value", "?")
    contracts = getattr(trade, "contracts", "?")
    entry = getattr(trade, "entry_price", "?")
    exit_price = getattr(trade, "exit_price", "?")
    net = getattr(trade, "net_pnl", None)
    reason = getattr(getattr(trade, "close_reason", None), "value", "")
    net_text = f"{net:+.2f}" if net is not None else "?"
    outcome = "WIN" if (net is not None and net > 0) else "loss"
    return (f"MNQ paper {outcome}: {direction} {contracts} @ {entry} -> {exit_price} "
            f"| net ${net_text} ({reason})")


def _http_send(token: str, chat_id: str, text: str) -> None:
    """POST one message to the Telegram Bot API (no third-party deps)."""
    import json
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed api host
        response.read()
