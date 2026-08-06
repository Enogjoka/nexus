"""
Acceptance tests for Task 17: the signed link.

Real sockets on loopback with ephemeral ports — no external network, and
nothing binds a fixed port that a developer's machine might already be using.

The governing property is that a frame which fails verification DOES NOT EXIST
as far as the application is concerned: not logged-and-passed, not delivered
with a warning flag, not delivered at all. Several tests therefore assert on
the callback NOT firing, which is the only assertion that actually proves it.

Verification failures are driven through a RAW socket rather than through
LinkClient. A tampered or replayed frame is by definition something a
well-behaved client would never send, so forging bytes directly is both more
honest and more precise than persuading the client to misbehave.
"""
import json
import logging
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import config
from link import channel as channel_mod
from link import messages
from link.channel import LinkClient, LinkServer
from link.messages import Envelope, Verifier

TEST_SECRET = "test-secret-not-a-real-key"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    """A throwaway signing key. Never a real secret, never from git."""
    monkeypatch.setattr(config, "HMAC_SECRET", TEST_SECRET)


def wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class Collector:
    """Thread-safe record of what a callback actually received."""

    def __init__(self):
        self.items = []
        self._lock = threading.Lock()

    def __call__(self, envelope):
        with self._lock:
            self.items.append(envelope)

    @property
    def count(self):
        with self._lock:
            return len(self.items)

    def seqs(self):
        with self._lock:
            return [e.seq for e in self.items]


@pytest.fixture
def server_factory():
    """Starts servers on ephemeral ports and guarantees they are torn down."""
    started = []

    def _make(**kwargs):
        kwargs.setdefault("host", "127.0.0.1")
        kwargs.setdefault("port", 0)
        kwargs.setdefault("heartbeat_seconds", 30)
        kwargs.setdefault("stale_seconds", 30)
        server = LinkServer(**kwargs)
        server.start()
        started.append(server)
        return server

    yield _make
    for server in started:
        server.stop()
        server.join(timeout=2)


@pytest.fixture
def client_factory():
    started = []

    def _make(**kwargs):
        kwargs.setdefault("host", "127.0.0.1")
        kwargs.setdefault("heartbeat_seconds", 30)
        kwargs.setdefault("stale_seconds", 30)
        kwargs.setdefault("reconnect_seconds", 0.05)
        client = LinkClient(**kwargs)
        client.start()
        started.append(client)
        return client

    yield _make
    for client in started:
        client.stop()
        client.join(timeout=2)


def raw_send(port, frames, linger=0.4):
    """Open a plain socket, write frames verbatim, keep it open briefly."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    try:
        for frame in frames:
            sock.sendall(frame + b"\n")
        time.sleep(linger)
    finally:
        sock.close()


# ===========================================================================
# round trip
# ===========================================================================


def test_doctrine_round_trip_and_ack(server_factory, client_factory):
    received = Collector()
    server = server_factory(on_doctrine=received)
    client = client_factory(port=server.port)

    assert wait_until(lambda: client.connected)
    seq = client.send("DOCTRINE", {"bias": "LONG_ONLY", "conviction": 7})

    assert wait_until(lambda: received.count == 1), "server never got the doctrine"
    envelope = received.items[0]
    assert envelope.kind == "DOCTRINE"
    assert envelope.seq == seq
    assert envelope.payload == {"bias": "LONG_ONLY", "conviction": 7}

    # The ACK came back and cleared the un-ACKed buffer.
    assert wait_until(lambda: client.unacked_count == 0), "ACK never cleared the buffer"


def test_swing_reaches_its_own_callback(server_factory, client_factory):
    doctrines, swings = Collector(), Collector()
    server = server_factory(on_doctrine=doctrines, on_swing=swings)
    client = client_factory(port=server.port)

    assert wait_until(lambda: client.connected)
    client.send("SWING", {"direction": "LONG"})

    assert wait_until(lambda: swings.count == 1)
    assert doctrines.count == 0


def test_exec_stream_reaches_the_brain(server_factory, client_factory):
    ticks = Collector()
    server = server_factory()
    client = client_factory(port=server.port, on_tick=ticks)

    assert wait_until(lambda: server.connected)
    assert server.send("TICK", {"bid": 4000.0, "ask": 4000.35})

    assert wait_until(lambda: ticks.count == 1)
    assert ticks.items[0].payload["bid"] == 4000.0


# ===========================================================================
# a frame that fails verification does not exist
# ===========================================================================


def test_tampered_frame_is_dropped_and_never_reaches_the_callback(server_factory, caplog):
    caplog.set_level(logging.INFO)
    received = Collector()
    server = server_factory(on_doctrine=received)

    # Sign a frame, then flip a byte inside the payload. Same length, still
    # valid JSON — so this reaches the mac check rather than dying as garbage.
    envelope = messages.make_envelope("DOCTRINE", 0, {"bias": "AAAA"})
    frame = messages.sign(envelope)
    tampered = frame.replace(b"AAAA", b"AAAB")
    assert tampered != frame and len(tampered) == len(frame)

    raw_send(server.port, [tampered])

    assert received.count == 0, "a tampered frame must never reach the application"
    assert any("SECURITY" in r.getMessage() and "mac mismatch" in r.getMessage()
               for r in caplog.records)


def test_replayed_frame_is_dropped(server_factory, caplog):
    caplog.set_level(logging.INFO)
    received = Collector()
    server = server_factory(on_doctrine=received)

    frame = messages.sign(messages.make_envelope("DOCTRINE", 5, {"bias": "FLAT"}))
    raw_send(server.port, [frame, frame])   # identical bytes, twice, one connection

    assert wait_until(lambda: received.count == 1)
    time.sleep(0.2)
    assert received.count == 1, "the replay must not be delivered a second time"
    assert any("REPLAY" in r.getMessage() for r in caplog.records)


def test_stale_clock_is_dropped(server_factory, caplog):
    caplog.set_level(logging.INFO)
    received = Collector()
    server = server_factory(on_doctrine=received)

    old = Envelope(
        kind="DOCTRINE",
        seq=0,
        sent_at=datetime.now(timezone.utc) - timedelta(seconds=120),
        payload={"bias": "FLAT"},
    )
    raw_send(server.port, [messages.sign(old)])

    assert received.count == 0
    assert any("SKEW" in r.getMessage() for r in caplog.records)


def test_future_clock_is_dropped_too(server_factory, caplog):
    """Skew is absolute — a peer running fast is as untrustworthy as one slow."""
    caplog.set_level(logging.INFO)
    received = Collector()
    server = server_factory(on_doctrine=received)

    ahead = Envelope(
        kind="DOCTRINE",
        seq=0,
        sent_at=datetime.now(timezone.utc) + timedelta(seconds=120),
        payload={"bias": "FLAT"},
    )
    raw_send(server.port, [messages.sign(ahead)])

    assert received.count == 0
    assert any("SKEW" in r.getMessage() for r in caplog.records)


def test_garbage_bytes_are_dropped(server_factory, caplog):
    caplog.set_level(logging.INFO)
    received = Collector()
    server = server_factory(on_doctrine=received)

    raw_send(server.port, [b"this is not json", b'{"envelope": {}}', b'{"mac": "x"}'])

    assert received.count == 0
    assert any("SECURITY" in r.getMessage() for r in caplog.records)


# ===========================================================================
# verifier unit level
# ===========================================================================


def test_verifier_accepts_a_good_frame():
    verifier = Verifier()
    envelope = messages.make_envelope("TICK", 0, {"bid": 1.0})
    assert verifier.verify(messages.sign(envelope)) is not None


def test_verifier_requires_strictly_increasing_seq():
    verifier = Verifier()
    assert verifier.verify(messages.sign(messages.make_envelope("TICK", 5, {}))) is not None
    assert verifier.verify(messages.sign(messages.make_envelope("TICK", 5, {}))) is None
    assert verifier.verify(messages.sign(messages.make_envelope("TICK", 4, {}))) is None
    assert verifier.verify(messages.sign(messages.make_envelope("TICK", 6, {}))) is not None


def test_a_skew_rejection_does_not_burn_a_sequence_number():
    """The real peer still needs that seq; a clock blip must not consume it."""
    verifier = Verifier()
    stale = Envelope(
        kind="TICK", seq=3,
        sent_at=datetime.now(timezone.utc) - timedelta(seconds=999), payload={},
    )
    assert verifier.verify(messages.sign(stale)) is None
    assert verifier.last_seq == -1
    assert verifier.verify(messages.sign(messages.make_envelope("TICK", 3, {}))) is not None


def test_a_mac_from_a_different_secret_is_rejected(monkeypatch):
    frame = messages.sign(messages.make_envelope("TICK", 0, {}))
    monkeypatch.setattr(config, "HMAC_SECRET", "a-completely-different-key")
    assert Verifier().verify(frame) is None


def test_envelope_is_frozen_and_forbids_extras():
    envelope = messages.make_envelope("TICK", 0, {})
    with pytest.raises(Exception):
        envelope.seq = 99
    with pytest.raises(Exception):
        Envelope(kind="TICK", seq=0, sent_at=datetime.now(timezone.utc), payload={}, extra=1)


def test_unknown_kind_is_rejected():
    with pytest.raises(Exception):
        Envelope(kind="SHUTDOWN_EVERYTHING", seq=0, sent_at=datetime.now(timezone.utc), payload={})


def test_negative_seq_is_rejected():
    with pytest.raises(Exception):
        Envelope(kind="TICK", seq=-1, sent_at=datetime.now(timezone.utc), payload={})


def test_naive_timestamp_is_rejected(caplog):
    caplog.set_level(logging.INFO)
    naive = Envelope(kind="TICK", seq=0, sent_at=datetime(2026, 8, 6, 12, 0), payload={})
    assert Verifier().verify(messages.sign(naive)) is None
    assert any("SKEW" in r.getMessage() for r in caplog.records)


def test_canonical_bytes_are_order_independent():
    """Both ends must serialise identical bytes or every frame fails."""
    assert messages.canonical_bytes({"a": 1, "b": 2}) == messages.canonical_bytes({"b": 2, "a": 1})


# ===========================================================================
# no secret, no link
# ===========================================================================


@pytest.mark.parametrize("secret", [None, ""])
def test_everything_refuses_to_start_without_a_secret(monkeypatch, secret):
    monkeypatch.setattr(config, "HMAC_SECRET", secret)

    with pytest.raises(RuntimeError, match="refuses to start"):
        LinkServer(host="127.0.0.1", port=0)
    with pytest.raises(RuntimeError, match="refuses to start"):
        LinkClient(host="127.0.0.1", port=1)
    with pytest.raises(RuntimeError, match="refuses to start"):
        Verifier()
    with pytest.raises(RuntimeError, match="refuses to start"):
        messages.sign(Envelope(kind="TICK", seq=0, sent_at=datetime.now(timezone.utc), payload={}))


def test_the_refusal_says_why():
    import inspect

    source = inspect.getsource(messages.require_secret)
    assert "not a degraded mode" in source


def test_mac_comparison_is_constant_time():
    """
    A byte-by-byte compare leaks how much of a forged mac was right, which is
    enough to forge the rest one byte at a time.
    """
    import inspect

    source = inspect.getsource(messages.Verifier.verify)
    assert "hmac.compare_digest" in source
    assert "==" not in source.split("compare_digest")[0].split("expected")[-1]


# ===========================================================================
# reconnect, resend, exactly-once
# ===========================================================================


def test_seq_continues_across_a_reconnect_and_unacked_are_resent(client_factory):
    received = Collector()
    server = LinkServer(host="127.0.0.1", port=0, on_doctrine=received,
                        heartbeat_seconds=30, stale_seconds=30)
    port = server.start()
    try:
        client = client_factory(port=port)
        assert wait_until(lambda: client.connected)

        first = client.send("DOCTRINE", {"n": 1})
        assert wait_until(lambda: received.count == 1)
        assert wait_until(lambda: client.unacked_count == 0)
    finally:
        server.stop()
        server.join(timeout=2)

    assert wait_until(lambda: not client.connected, timeout=5)

    # Sent while the exec box is down: buffered, not lost, not an error.
    second = client.send("DOCTRINE", {"n": 2})
    third = client.send("DOCTRINE", {"n": 3})
    assert client.unacked_count == 2
    assert (second, third) == (first + 1, first + 2), "seq must not reset on disconnect"

    restarted = LinkServer(host="127.0.0.1", port=port, on_doctrine=received,
                           heartbeat_seconds=30, stale_seconds=30)
    restarted.start()
    try:
        assert wait_until(lambda: received.count == 3, timeout=10), (
            f"resend never arrived; got {received.seqs()}"
        )
        assert wait_until(lambda: client.unacked_count == 0, timeout=10)
        assert received.seqs() == [first, second, third]
    finally:
        restarted.stop()
        restarted.join(timeout=2)


def test_each_seq_is_delivered_exactly_once_across_a_restart(client_factory):
    received = Collector()
    port_holder = LinkServer(host="127.0.0.1", port=0, heartbeat_seconds=30, stale_seconds=30)
    port = port_holder.start()
    port_holder.stop()
    port_holder.join(timeout=2)

    client = client_factory(port=port)
    # The exec box is down: everything buffers.
    seqs = [client.send("DOCTRINE", {"n": i}) for i in range(5)]
    assert client.unacked_count == 5

    server = LinkServer(host="127.0.0.1", port=port, on_doctrine=received,
                        heartbeat_seconds=30, stale_seconds=30)
    server.start()
    try:
        assert wait_until(lambda: received.count == 5, timeout=10), received.seqs()
        time.sleep(0.4)  # give any duplicate a chance to show up
        assert received.seqs() == seqs, "each seq exactly once, in order"
    finally:
        server.stop()
        server.join(timeout=2)


def test_a_second_connection_replaces_the_first(server_factory):
    received = Collector()
    server = server_factory(on_doctrine=received)

    first = socket.create_connection(("127.0.0.1", server.port), timeout=2.0)
    assert wait_until(lambda: server.connected)

    second = socket.create_connection(("127.0.0.1", server.port), timeout=2.0)
    try:
        # The new connection works, which is what "the brain reconnected" means.
        second.sendall(messages.sign(messages.make_envelope("DOCTRINE", 0, {"n": 2})) + b"\n")
        assert wait_until(lambda: received.count == 1)
        assert received.items[0].payload == {"n": 2}
    finally:
        first.close()
        second.close()


# ===========================================================================
# staleness
# ===========================================================================


def test_server_fires_connection_stale_when_the_brain_goes_quiet(server_factory):
    fired = threading.Event()
    server = server_factory(
        on_stale=fired.set, heartbeat_seconds=30, stale_seconds=0.4
    )

    sock = socket.create_connection(("127.0.0.1", server.port), timeout=2.0)
    try:
        assert wait_until(lambda: server.connected)
        assert fired.wait(timeout=5.0), "silence past stale_seconds must fire the callback"
    finally:
        sock.close()


def test_client_fires_connection_stale_when_exec_goes_quiet(server_factory, client_factory):
    fired = threading.Event()
    server = server_factory(heartbeat_seconds=30)
    client = client_factory(port=server.port, on_stale=fired.set,
                            heartbeat_seconds=30, stale_seconds=0.4)

    assert wait_until(lambda: client.connected)
    assert fired.wait(timeout=5.0)


def test_a_healthy_heartbeat_keeps_the_link_fresh(server_factory, client_factory):
    """The inverse: with heartbeats flowing, stale must NOT fire."""
    fired = threading.Event()
    server = server_factory(heartbeat_seconds=0.1, stale_seconds=1.5, on_stale=fired.set)
    client = client_factory(port=server.port, heartbeat_seconds=0.1, stale_seconds=1.5)

    assert wait_until(lambda: client.connected)
    assert not fired.wait(timeout=2.0), "heartbeats should have kept the link alive"


# ===========================================================================
# bounded buffer
# ===========================================================================


def test_unacked_buffer_drops_oldest_past_the_bound(monkeypatch, caplog, client_factory):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(channel_mod, "MAX_UNACKED", 5)

    # Port 1 is never listening, so nothing is ever ACKed.
    client = client_factory(port=1)
    for i in range(8):
        client.send("DOCTRINE", {"n": i})

    assert client.unacked_count == 5, "the buffer must stay bounded"
    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert criticals, "dropping a message must be CRITICAL, never quiet"
    assert "DROPPING seq 0" in criticals[0].getMessage(), "oldest goes first"


def test_the_real_bound_is_one_thousand():
    assert channel_mod.MAX_UNACKED == 1000


# ===========================================================================
# disconnect semantics are documented, not invented
# ===========================================================================


def test_the_channel_documents_why_it_does_nothing_on_link_loss():
    import inspect

    doc = inspect.getdoc(channel_mod) or ""
    assert "NOTHING special" in doc
    assert "FLAT" in doc, "the docstring must point at the property it relies on"


def test_no_websocket_or_grpc_dependency():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(channel_mod.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    for module in imported:
        root = module.split(".")[0]
        assert root not in ("websockets", "websocket", "grpc", "aiohttp", "tornado")
    assert "socket" in imported, "the transport is stdlib sockets"
