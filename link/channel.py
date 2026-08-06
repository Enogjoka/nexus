"""
The nervous system: a signed TCP channel between the brain box and the exec box.

WHY PLAIN TCP AND NOT WEBSOCKETS OR gRPC
This is a private point-to-point pipe between two machines we own, carrying
newline-delimited JSON frames that are already authenticated by HMAC. A
websocket adds an HTTP upgrade handshake, framing, masking and a third-party
dependency to solve problems we do not have (browser compatibility, multiplexed
subprotocols, proxy traversal). gRPC adds a compiler and a schema toolchain to
solve a problem pydantic already solves here. stdlib socket has no supply chain,
no version drift, and nothing to keep patched — for a two-node link whose
security lives in the mac rather than the transport, that is the right trade.
The channel stays deliberately dumb so the interesting part stays in messages.py.

TOPOLOGY
    LinkClient  (brain side, this box)  -> connects, sends DOCTRINE / SWING
    LinkServer  (exec side, Windows)    -> accepts, sends TICK / FILL / KERNEL_EVENT
Both ends heartbeat and both ends notice silence.

DISCONNECT SEMANTICS — SILENCE IS ALREADY SAFE, SO THIS TASK REFUSES TO FIGHT IT
The exec box owns positions and the kernel. When the link goes stale it does
NOTHING special: it does not flatten, it does not halt, it does not panic. It
does not need to. Task 15 already built the property that matters — the
DoctrineHolder expires its posture and degrades to FLAT, which disarms the pods,
while the kernel keeps guarding every order regardless of whether anyone is
talking to it. A link-loss reaction that flattened positions would be a NEW
failure mode invented on top of a system that already fails safe, and it would
fire on every transient network blip.

The brain box, having no positions, simply queues what it could not send and
resends the un-ACKed remainder on reconnect. It reconciles; it does not act.

ONE CONNECTION AT A TIME. A second connection to the server closes the first.
The brain reconnecting after a network drop is the expected case, and the old
half-open socket must not linger holding the slot the live brain needs.

NOT WIRED. Neither end is registered in backend.py. Both sit idle until the
boxes actually split; Task 22 registers them when there is a second box to talk
to.
"""
import logging
import socket
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional

import config
from link import messages
from link.messages import Envelope, Verifier

logger = logging.getLogger(__name__)

# Past this many un-ACKed frames the brain is talking to a wall. Dropping the
# OLDEST is deliberate: in a trading system the newest posture supersedes the
# stale one, so if something must be lost it should be the message that is
# already out of date.
MAX_UNACKED = 1000

_SOCKET_POLL_SECONDS = 0.5   # how often blocked reads check the stop flag
_RECONNECT_SECONDS = 1.0     # client backoff between connection attempts


class _Framed:
    """
    Newline-delimited frames over a socket.

    Frames are bounded by MAX_FRAME_BYTES so a peer that never sends a newline
    cannot grow our buffer without limit — an unauthenticated party can reach
    this code, since the mac is only checked after a full frame is assembled.
    """

    MAX_FRAME_BYTES = 1_048_576

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = b""
        self._send_lock = threading.Lock()

    def send_frame(self, data: bytes) -> None:
        with self._send_lock:
            self._sock.sendall(data + b"\n")

    def recv_frame(self) -> Optional[bytes]:
        """
        Next complete frame, or None when the peer has gone away. Raises
        socket.timeout when nothing arrived within the socket's timeout, which
        the caller uses as its cue to re-check the stop flag.
        """
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                frame, self._buffer = self._buffer[:newline], self._buffer[newline + 1:]
                return frame

            if len(self._buffer) > self.MAX_FRAME_BYTES:
                logger.error("link: peer sent %d bytes with no frame delimiter; dropping them",
                             len(self._buffer))
                self._buffer = b""
                return None

            chunk = self._sock.recv(65536)
            if not chunk:
                return None
            self._buffer += chunk

    def close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class _Endpoint:
    """
    Shared machinery: outbound sequencing, heartbeat, staleness watch.

    Both ends do the same three things — send with a monotonic seq, emit a
    heartbeat on a timer, and notice when the peer stops talking — so they are
    written once here rather than twice with a subtle difference between them.
    """

    def __init__(
        self,
        name: str,
        heartbeat_seconds: Optional[float],
        stale_seconds: Optional[float],
        on_stale: Optional[Callable[[], None]],
    ) -> None:
        # Refuse to construct without a signing key (INVARIANT: no unsigned mode).
        messages.require_secret()

        self.name = name
        self.heartbeat_seconds = (
            config.LINK_HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds
        )
        self.stale_seconds = (
            config.LINK_STALE_SECONDS if stale_seconds is None else stale_seconds
        )
        self._on_stale = on_stale

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._seq = 0
        self._last_rx = time.monotonic()
        self._stale_fired = False
        self._threads: list = []
        self._threads_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def _spawn(self, target, thread_name: str) -> threading.Thread:
        thread = threading.Thread(target=self._guard(target), name=thread_name, daemon=True)
        with self._threads_lock:
            # Prune finished threads so a long-lived endpoint that has survived
            # many reconnects does not accumulate dead Thread objects forever.
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()
        return thread

    def _guard(self, target):
        """Survival contract: a thread that dies must say why, and only once."""

        def runner():
            try:
                target()
            except Exception:
                logger.exception("%s: thread %s died", self.name, threading.current_thread().name)

        return runner

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def join(self, timeout: float = 5.0) -> None:
        with self._threads_lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=timeout)

    # -- sequencing --------------------------------------------------------

    def next_seq(self) -> int:
        with self._lock:
            seq = self._seq
            self._seq += 1
            return seq

    # -- liveness ----------------------------------------------------------

    def mark_rx(self) -> None:
        with self._lock:
            self._last_rx = time.monotonic()
            self._stale_fired = False

    def _check_stale(self) -> bool:
        """Fire on_stale at most once per silent stretch."""
        with self._lock:
            silent_for = time.monotonic() - self._last_rx
            if silent_for <= self.stale_seconds or self._stale_fired:
                return False
            self._stale_fired = True

        logger.error(
            "%s: peer silent for %.1fs (limit %ss) — link is stale",
            self.name, silent_for, self.stale_seconds,
        )
        if self._on_stale is not None:
            try:
                self._on_stale()
            except Exception:
                logger.exception("%s: connection_stale callback raised", self.name)
        return True


class LinkServer(_Endpoint):
    """
    Exec side. Accepts ONE brain connection at a time; a second accept closes
    the first, because a reconnecting brain must be able to take back the slot
    its own dead socket is holding.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        on_doctrine: Optional[Callable[[Envelope], None]] = None,
        on_swing: Optional[Callable[[Envelope], None]] = None,
        on_stale: Optional[Callable[[], None]] = None,
        heartbeat_seconds: Optional[float] = None,
        stale_seconds: Optional[float] = None,
    ) -> None:
        super().__init__("link-server", heartbeat_seconds, stale_seconds, on_stale)
        self.host = config.LINK_HOST if host is None else host
        self.port = config.LINK_PORT if port is None else port
        self.on_doctrine = on_doctrine
        self.on_swing = on_swing

        self._listener: Optional[socket.socket] = None
        self._conn: Optional[_Framed] = None
        self._conn_lock = threading.Lock()

    def start(self) -> int:
        """Bind, listen, and serve. Returns the bound port (port=0 picks one)."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(2)
        listener.settimeout(_SOCKET_POLL_SECONDS)
        self._listener = listener
        self.port = listener.getsockname()[1]

        logger.info("link-server: listening on %s:%d", self.host, self.port)
        self._spawn(self._accept_loop, "link-server-accept")
        self._spawn(self._heartbeat_loop, "link-server-heartbeat")
        return self.port

    def stop(self) -> None:
        super().stop()
        with self._conn_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass

    # -- accept / serve ----------------------------------------------------

    def _accept_loop(self) -> None:
        while not self.stopped:
            try:
                sock, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self.stopped:
                    logger.exception("link-server: accept failed")
                return

            logger.info("link-server: brain connected from %s", addr)
            sock.settimeout(_SOCKET_POLL_SECONDS)
            framed = _Framed(sock)

            with self._conn_lock:
                previous = self._conn
                self._conn = framed
            if previous is not None:
                logger.warning("link-server: replacing the previous connection")
                previous.close()

            self.mark_rx()
            # Serve on its own thread: if the accept loop served inline it
            # would be blocked for the life of the connection, and a brain
            # reconnecting after a half-open socket could never take back the
            # slot its own dead connection is holding — the exact case this
            # server is supposed to handle.
            self._spawn(lambda f=framed: self._serve(f), "link-server-conn")

    def _serve(self, framed: _Framed) -> None:
        """Read frames until the peer goes away. One Verifier per connection."""
        verifier = Verifier(peer="brain")
        while not self.stopped:
            try:
                raw = framed.recv_frame()
            except socket.timeout:
                with self._conn_lock:
                    superseded = self._conn is not framed
                if superseded:
                    break  # a newer connection owns the slot; retire quietly
                if self._check_stale():
                    # The brain has gone quiet; drop the socket and wait for it
                    # to come back rather than holding a corpse.
                    break
                continue
            except OSError:
                break

            if raw is None:
                logger.info("link-server: brain disconnected")
                break
            if not raw.strip():
                continue

            envelope = verifier.verify(raw)
            if envelope is None:
                continue  # already logged; the frame never existed

            self.mark_rx()
            self._dispatch(envelope, framed)

        framed.close()
        with self._conn_lock:
            if self._conn is framed:
                self._conn = None

    def _dispatch(self, envelope: Envelope, framed: _Framed) -> None:
        if envelope.kind == "HEARTBEAT":
            return  # liveness only; mark_rx already recorded it

        handler = {"DOCTRINE": self.on_doctrine, "SWING": self.on_swing}.get(envelope.kind)
        if handler is not None:
            try:
                handler(envelope)
            except Exception:
                logger.exception("link-server: %s handler raised", envelope.kind)
        elif envelope.kind not in ("ACK",):
            logger.warning("link-server: no handler for %s", envelope.kind)

        # ACK every accepted message, whether or not a handler existed: the ACK
        # means "verified and delivered", not "acted upon".
        self._send_on(framed, "ACK", {"ack_seq": envelope.seq})

    # -- outbound ----------------------------------------------------------

    def _send_on(self, framed: _Framed, kind: str, payload: Dict[str, Any]) -> bool:
        envelope = messages.make_envelope(kind, self.next_seq(), payload)
        try:
            framed.send_frame(messages.sign(envelope))
            return True
        except OSError:
            logger.warning("link-server: send failed for %s", kind)
            return False

    def send(self, kind: str, payload: Optional[Dict[str, Any]] = None) -> bool:
        """
        Send to the connected brain. Returns False when nobody is connected —
        the exec box does not buffer, because a TICK the brain missed is stale
        by the time it reconnects and a fresher one is already on its way.
        """
        with self._conn_lock:
            framed = self._conn
        if framed is None:
            return False
        return self._send_on(framed, kind, payload or {})

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            with self._conn_lock:
                framed = self._conn
            if framed is not None:
                self._send_on(framed, "HEARTBEAT", {})
            self._check_stale()

    @property
    def connected(self) -> bool:
        with self._conn_lock:
            return self._conn is not None


class LinkClient(_Endpoint):
    """
    Brain side. Connects, sends with a monotonic seq that survives reconnects,
    and resends whatever the exec box never ACKed.

    The seq counter does NOT reset on reconnect: it identifies a message across
    the life of the process, so a resent frame is recognisably the same message
    rather than a new one wearing an old number.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        on_tick: Optional[Callable[[Envelope], None]] = None,
        on_fill: Optional[Callable[[Envelope], None]] = None,
        on_kernel_event: Optional[Callable[[Envelope], None]] = None,
        on_stale: Optional[Callable[[], None]] = None,
        heartbeat_seconds: Optional[float] = None,
        stale_seconds: Optional[float] = None,
        reconnect_seconds: float = _RECONNECT_SECONDS,
    ) -> None:
        super().__init__("link-client", heartbeat_seconds, stale_seconds, on_stale)
        self.host = config.LINK_HOST if host is None else host
        self.port = config.LINK_PORT if port is None else port
        self.on_tick = on_tick
        self.on_fill = on_fill
        self.on_kernel_event = on_kernel_event
        self.reconnect_seconds = reconnect_seconds

        self._conn: Optional[_Framed] = None
        self._conn_lock = threading.Lock()
        # seq -> signed frame, in send order. Cleared entry by entry as ACKs land.
        self._unacked: "OrderedDict[int, bytes]" = OrderedDict()

    def start(self) -> None:
        self._spawn(self._connect_loop, "link-client-connect")
        self._spawn(self._heartbeat_loop, "link-client-heartbeat")

    def stop(self) -> None:
        super().stop()
        with self._conn_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- connection --------------------------------------------------------

    def _connect_loop(self) -> None:
        while not self.stopped:
            try:
                sock = socket.create_connection((self.host, self.port), timeout=2.0)
            except OSError:
                # Expected while the exec box is down; the loop IS the retry.
                if self._stop.wait(self.reconnect_seconds):
                    return
                continue

            logger.info("link-client: connected to %s:%d", self.host, self.port)
            sock.settimeout(_SOCKET_POLL_SECONDS)
            framed = _Framed(sock)
            with self._conn_lock:
                self._conn = framed
            self.mark_rx()

            self._flush_unacked(framed)
            self._read_until_closed(framed)

            framed.close()
            with self._conn_lock:
                if self._conn is framed:
                    self._conn = None
            logger.warning("link-client: disconnected; will retry")
            if self._stop.wait(self.reconnect_seconds):
                return

    def _read_until_closed(self, framed: _Framed) -> None:
        verifier = Verifier(peer="exec")
        while not self.stopped:
            try:
                raw = framed.recv_frame()
            except socket.timeout:
                if self._check_stale():
                    break  # force a reconnect rather than trust a silent socket
                continue
            except OSError:
                break

            if raw is None:
                break
            if not raw.strip():
                continue

            envelope = verifier.verify(raw)
            if envelope is None:
                continue

            self.mark_rx()
            self._dispatch(envelope)

    def _dispatch(self, envelope: Envelope) -> None:
        if envelope.kind == "ACK":
            acked = envelope.payload.get("ack_seq")
            if isinstance(acked, int):
                with self._lock:
                    self._unacked.pop(acked, None)
            return
        if envelope.kind == "HEARTBEAT":
            return

        handler = {
            "TICK": self.on_tick,
            "FILL": self.on_fill,
            "KERNEL_EVENT": self.on_kernel_event,
        }.get(envelope.kind)
        if handler is None:
            logger.warning("link-client: no handler for %s", envelope.kind)
            return
        try:
            handler(envelope)
        except Exception:
            logger.exception("link-client: %s handler raised", envelope.kind)

    # -- outbound ----------------------------------------------------------

    def send(self, kind: str, payload: Optional[Dict[str, Any]] = None) -> int:
        """
        Queue a message and send it if we are connected. Returns its seq.

        A send while disconnected is not an error: the frame sits in the
        un-ACKed buffer and goes out on reconnect. That buffering IS the
        brain-side disconnect behaviour — queue, then reconcile.
        """
        seq = self.next_seq()
        frame = messages.sign(messages.make_envelope(kind, seq, payload or {}))

        with self._lock:
            self._unacked[seq] = frame
            while len(self._unacked) > MAX_UNACKED:
                dropped, _ = self._unacked.popitem(last=False)
                logger.critical(
                    "link-client: un-ACKed buffer full (%d); DROPPING seq %d — the exec "
                    "box has not acknowledged anything for a long time and messages are "
                    "now being lost",
                    MAX_UNACKED, dropped,
                )

        with self._conn_lock:
            framed = self._conn
        if framed is not None:
            try:
                framed.send_frame(frame)
            except OSError:
                logger.warning("link-client: send failed for seq %d; buffered for retry", seq)
        return seq

    def _flush_unacked(self, framed: _Framed) -> None:
        """Resend everything the exec box never acknowledged, in seq order."""
        with self._lock:
            pending = list(self._unacked.items())
        if not pending:
            return
        logger.info("link-client: resending %d un-ACKed frame(s)", len(pending))
        for seq, frame in pending:
            try:
                framed.send_frame(frame)
            except OSError:
                logger.warning("link-client: resend failed at seq %d", seq)
                return

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            with self._conn_lock:
                framed = self._conn
            if framed is not None:
                envelope = messages.make_envelope("HEARTBEAT", self.next_seq(), {})
                try:
                    framed.send_frame(messages.sign(envelope))
                except OSError:
                    pass
            self._check_stale()

    @property
    def connected(self) -> bool:
        with self._conn_lock:
            return self._conn is not None

    @property
    def unacked_count(self) -> int:
        with self._lock:
            return len(self._unacked)
