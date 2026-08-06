"""
The wire contract between the brain box and the exec box.

Every frame on this channel is signed. There is no unsigned mode, no "trusted
network" flag, and no development shortcut that skips verification — if
HMAC_SECRET is unset the link raises at construction rather than starting.
An unsigned channel is not a degraded mode, it is no channel: the messages
that cross this wire tell one machine to take positions on another, and a wire
that cannot prove who spoke is worse than no wire at all, because it looks
like one that works.

FRAME FORMAT
    {"envelope": <body>, "mac": "<hex>"}
where <body> is the Envelope's JSON form and the mac is
hmac_sha256(HMAC_SECRET, canonical_json(<body>)). Canonical means sorted keys
and tight separators, so both ends serialise the identical bytes and the mac
is reproducible. The transport delimits frames with newlines, which is safe
because JSON escapes any newline inside a string.

FOUR WAYS A FRAME DIES, AND ALL OF THEM ARE SILENT TO THE APPLICATION
    SECURITY  malformed, unparseable, or the mac does not match
    REPLAY    seq not strictly greater than the last accepted from this peer
    SKEW      sent_at too far from our clock in either direction
Each is logged loudly and returns None. There is deliberately no path where a
verification failure yields a message the caller can act on: verify() returns
Optional[Envelope] and None means the frame never existed.

REPLAY PROTECTION IS PER CONNECTION. A Verifier holds the last accepted seq for
one peer on one connection, which is why the channel builds a fresh Verifier
per accepted socket. Across a reconnect the counter restarts, which is correct:
the client resends un-ACKed frames after a drop, and those must be allowed
through exactly once on the new connection.
"""
import hmac
import json
import logging
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

import config

logger = logging.getLogger(__name__)

KINDS = (
    "DOCTRINE",
    "SWING",
    "TICK",
    "FILL",
    "KERNEL_EVENT",
    "HEARTBEAT",
    "ACK",
)


class Envelope(BaseModel):
    """
    One message on the wire. Frozen so a verified envelope cannot be edited
    after the mac that vouched for it has been checked.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    kind: Literal["DOCTRINE", "SWING", "TICK", "FILL", "KERNEL_EVENT", "HEARTBEAT", "ACK"]
    seq: int = Field(ge=0)
    sent_at: datetime
    payload: Dict[str, Any] = Field(default_factory=dict)


def require_secret() -> bytes:
    """
    The signing key, or a refusal. Called at construction by every component
    that touches the wire, so an unconfigured link fails at startup rather
    than at the first message.
    """
    secret = config.HMAC_SECRET
    if not secret:
        raise RuntimeError(
            "HMAC_SECRET is not set — the NEXUS link refuses to start. "
            "An unsigned channel is not a degraded mode, it is no channel. "
            "Set HMAC_SECRET in the environment on BOTH boxes (never in git, "
            "never in the database) and restart."
        )
    return secret.encode("utf-8")


def canonical_bytes(body: Dict[str, Any]) -> bytes:
    """
    The exact bytes the mac is computed over.

    sort_keys makes the ordering independent of how the dict was built, and
    the tight separators remove the whitespace that json.dumps would otherwise
    vary. Both ends must produce identical bytes for identical content or every
    frame would fail verification.
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_mac(body: Dict[str, Any], secret: Optional[bytes] = None) -> str:
    return hmac.new(secret or require_secret(), canonical_bytes(body), sha256).hexdigest()


def make_envelope(kind: str, seq: int, payload: Optional[Dict[str, Any]] = None) -> Envelope:
    return Envelope(
        kind=kind, seq=seq, sent_at=datetime.now(timezone.utc), payload=payload or {}
    )


def sign(envelope: Envelope) -> bytes:
    """
    Serialise and sign. Returns the frame WITHOUT its trailing newline — the
    transport owns delimiting, so this function stays usable for tests that
    want to inspect or corrupt the bytes.
    """
    body = envelope.model_dump(mode="json")
    frame = {"envelope": body, "mac": compute_mac(body)}
    return json.dumps(frame, sort_keys=True, separators=(",", ":")).encode("utf-8")


class Verifier:
    """
    Verifies frames from ONE peer on ONE connection.

    Holds the replay counter, which is why it is an object rather than a
    function: "strictly greater than the last accepted seq" is meaningless
    without somewhere to remember that seq.
    """

    def __init__(self, peer: str = "peer") -> None:
        # Refuse to exist without a key, so a misconfigured link cannot get as
        # far as receiving a frame it is unable to check.
        self._secret = require_secret()
        self._peer = peer
        self._last_seq = -1

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def verify(self, raw: bytes) -> Optional[Envelope]:
        """
        Return the envelope only if the frame is authentic, fresh and new.
        Every rejection logs its category and returns None.
        """
        try:
            frame = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            logger.error("SECURITY [%s]: frame is not valid JSON (%s)", self._peer, exc)
            return None

        if not isinstance(frame, dict):
            logger.error("SECURITY [%s]: frame is not a JSON object", self._peer)
            return None

        body = frame.get("envelope")
        mac = frame.get("mac")
        if not isinstance(body, dict) or not isinstance(mac, str):
            logger.error("SECURITY [%s]: frame is missing its envelope or mac", self._peer)
            return None

        expected = hmac.new(self._secret, canonical_bytes(body), sha256).hexdigest()
        # Constant time: a byte-by-byte comparison leaks how much of a forged
        # mac was correct, which is enough to forge the rest one byte at a time.
        if not hmac.compare_digest(expected, mac):
            logger.error(
                "SECURITY [%s]: mac mismatch — frame was forged or tampered with; dropping",
                self._peer,
            )
            return None

        try:
            envelope = Envelope.model_validate(body)
        except Exception as exc:
            # A correctly-signed but malformed body means our own peer is
            # broken, not that someone is attacking us — but it is still a
            # frame we refuse to act on.
            logger.error("SECURITY [%s]: envelope failed validation (%s)", self._peer, exc)
            return None

        if envelope.seq <= self._last_seq:
            logger.error(
                "REPLAY [%s]: seq %d is not greater than the last accepted %d; dropping",
                self._peer, envelope.seq, self._last_seq,
            )
            return None

        sent_at = envelope.sent_at
        if sent_at.tzinfo is None:
            logger.error("SKEW [%s]: sent_at has no timezone; dropping", self._peer)
            return None

        skew = abs((datetime.now(timezone.utc) - sent_at).total_seconds())
        if skew > config.LINK_MAX_CLOCK_SKEW_SECONDS:
            logger.error(
                "SKEW [%s]: sent_at is %.1fs from our clock (max %ss); dropping",
                self._peer, skew, config.LINK_MAX_CLOCK_SKEW_SECONDS,
            )
            return None

        # Advance ONLY on full acceptance, so a frame rejected for skew cannot
        # burn a sequence number the real peer still needs.
        self._last_seq = envelope.seq
        return envelope
