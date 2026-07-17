"""Consume the Java bridge handshake: protocol version, IDs, declared capabilities.

The Java forwarder now sends a versioned handshake on connect and carries a
stream id (stable per JVM load) and a connection id (new per connection) on every
control message. This is the receiver's side of that contract:

* refuse an incompatible bridge instead of silently misparsing it,
* tell a reconnect (same stream, new connection) from a genuinely new bridge,
* learn what the feed declares it can supply, complementing what the receiver
  later *observes* on the wire (see :mod:`app.market.capabilities`).

A declaration is a promise, not proof. The observed capability model remains the
source of truth for whether trades are actually arriving; this only records what
the bridge claims, so a mismatch between claim and observation is detectable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# The protocol version this receiver understands. A bridge advertising a
# different MAJOR version is refused; a newer MINOR is accepted (additive).
SUPPORTED_PROTOCOL_MAJOR = 1


@dataclass(frozen=True, slots=True)
class BridgeHandshake:
    """A parsed ``connected`` handshake from the Java forwarder."""

    protocol_version: str
    stream_id: str
    connection_id: str
    session_id: str
    declared_capabilities: tuple[str, ...]
    provider: str
    compatible: bool
    reason: str

    @property
    def declares(self) -> frozenset[str]:
        """The set of capability ids the bridge claims to supply."""
        return frozenset(self.declared_capabilities)


def parse_handshake(event: Mapping[str, object]) -> BridgeHandshake:
    """Parse a ``connected`` control event into a typed handshake.

    Missing fields are tolerated (older bridges predate them) and surface as an
    explicit incompatibility rather than an exception.
    """
    version = str(event.get("protocol_version", "")).strip()
    capabilities = tuple(
        part.strip()
        for part in str(event.get("capabilities", "")).split(",")
        if part.strip()
    )
    compatible, reason = _check_compatibility(version)
    return BridgeHandshake(
        protocol_version=version,
        stream_id=str(event.get("stream_id", "")),
        connection_id=str(event.get("connection_id", "")),
        session_id=str(event.get("session_id", "")),
        declared_capabilities=capabilities,
        provider=str(event.get("provider", "")),
        compatible=compatible,
        reason=reason,
    )


def _check_compatibility(version: str) -> tuple[bool, str]:
    if not version:
        return False, "bridge sent no protocol_version (pre-handshake build)"
    major = version.split(".", 1)[0]
    if not major.isdigit():
        return False, f"unparseable protocol_version {version!r}"
    if int(major) != SUPPORTED_PROTOCOL_MAJOR:
        return False, (
            f"bridge protocol {version} is incompatible with receiver "
            f"major {SUPPORTED_PROTOCOL_MAJOR}"
        )
    return True, f"protocol {version} accepted"


class ConnectionTracker:
    """Detects reconnects and new bridges from successive handshakes.

    A reconnect is the same ``stream_id`` with a new ``connection_id``. A new
    ``stream_id`` is a different bridge process entirely — which, mid-session,
    means the previous stream ended without a clean marker.
    """

    def __init__(self) -> None:
        """Start with no prior handshake seen."""
        self._stream_id: str = ""
        self._connection_id: str = ""
        self.reconnects = 0
        self.new_streams = 0

    def observe(self, handshake: BridgeHandshake) -> str:
        """Record a handshake and classify it: 'initial' | 'reconnect' | 'new_stream'."""
        if not self._stream_id:
            self._stream_id, self._connection_id = handshake.stream_id, handshake.connection_id
            return "initial"
        if handshake.stream_id != self._stream_id:
            self._stream_id, self._connection_id = handshake.stream_id, handshake.connection_id
            self.new_streams += 1
            return "new_stream"
        if handshake.connection_id != self._connection_id:
            self._connection_id = handshake.connection_id
            self.reconnects += 1
            return "reconnect"
        return "duplicate"
