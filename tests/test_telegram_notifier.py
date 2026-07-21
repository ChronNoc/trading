"""Telegram trade alerts: opt-in, non-blocking, token-safe. No real network."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.notify.telegram import CHAT_ENV, TOKEN_ENV, TelegramNotifier, format_trade


class _Trade:
    """Minimal stand-in for a closed PaperTrade."""

    class _Enum:
        def __init__(self, value: str) -> None:
            self.value = value

    def __init__(self, direction: str, net: Decimal, reason: str = "target") -> None:
        self.direction = self._Enum(direction)
        self.contracts = 1
        self.entry_price = Decimal("29191.50")
        self.exit_price = Decimal("29200.00")
        self.net_pnl = net
        self.close_reason = self._Enum(reason)


def test_disabled_without_credentials_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(CHAT_ENV, raising=False)
    calls: list[tuple[str, str, str]] = []
    notifier = TelegramNotifier(sender=lambda t, c, m: calls.append((t, c, m)))
    assert notifier.enabled is False
    notifier.notify_trade(_Trade("long", Decimal("15.76")))
    assert calls == [], "no credentials -> no send"


def test_enabled_delivers_a_formatted_message_to_the_chat() -> None:
    calls: list[tuple[str, str, str]] = []
    notifier = TelegramNotifier(token="tok-SECRET", chat_id="12345",
                                sender=lambda t, c, m: calls.append((t, c, m)))
    assert notifier.enabled is True
    ok = notifier._deliver(format_trade(_Trade("long", Decimal("15.76"))))
    assert ok is True
    assert len(calls) == 1
    token, chat, message = calls[0]
    assert token == "tok-SECRET" and chat == "12345"
    assert "long" in message and "15.76" in message and "WIN" in message


def test_message_text_never_contains_the_token() -> None:
    message = format_trade(_Trade("short", Decimal("-42.00"), reason="stop"))
    assert "tok-SECRET" not in message  # token is never part of the message body
    assert "loss" in message and "-42.00" in message and "stop" in message


def test_a_failing_send_never_raises_into_the_backend() -> None:
    def boom(_t: str, _c: str, _m: str) -> None:
        raise ConnectionError("network down")

    notifier = TelegramNotifier(token="t", chat_id="c", sender=boom)
    # _deliver swallows the error and reports failure instead of propagating.
    assert notifier._deliver("hi") is False
    # notify_trade (threaded) must also not raise.
    notifier.notify_trade(_Trade("long", Decimal("1.00")))


def test_credentials_come_from_env_when_not_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "env-tok")
    monkeypatch.setenv(CHAT_ENV, "env-chat")
    notifier = TelegramNotifier(sender=lambda t, c, m: None)
    assert notifier.enabled is True


def test_launchers_wire_the_notifier_but_gate_on_enabled() -> None:
    from pathlib import Path

    for launcher in ("tools/start_backend.py", "tools/start_assistant.py"):
        source = Path(launcher).read_text(encoding="utf-8")
        assert "TelegramNotifier()" in source
        assert "_trade_notifier.enabled" in source, "must not send unless configured"
