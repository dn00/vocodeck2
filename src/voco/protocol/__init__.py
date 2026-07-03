"""voco.protocol — the WS/bridge message vocabulary (SPEC §10).

ROLE: language-neutral event/command shapes + hand-rolled validators.
INVARIANTS: zero third-party imports; additive evolution only (never remove
or retype a field within v1); every turn-scoped event carries turn_id.
"""

from voco.protocol.messages import (
    PROTOCOL_VERSION,
    Envelope,
    make_event,
    validate_envelope,
)

__all__ = ["PROTOCOL_VERSION", "Envelope", "make_event", "validate_envelope"]
