"""
RING 2's output cage — the head of desk speaks in doctrine, never in orders.

INVARIANT 2 says the AI never emits raw prices or raw orders. This module is
how that is enforced for the strategic layer: Fable is handed state and may
return exactly one shape — a Doctrine of enums and bounded floats. A bias, a
conviction, a risk multiplier, which pods may run, and how long the posture is
good for. No entry, no stop, no size, no symbol. There is no field it could
put a price in even if it tried.

SILENCE DEGRADES TO FLAT. Every failure path in this file — API down, budget
exhausted, malformed JSON, out-of-range number, unknown pod, expired doctrine
— returns FLAT_FALLBACK: flat bias, zero conviction, zero risk multiplier, no
pods, no swing signals. A system that hears nothing from its strategist must
stand still, not improvise. There is deliberately no path here that turns a
failure into permission.

STRICTNESS IS THE CAGE, AND IT IS VALIDATED IN JSON MODE.
Doctrine is frozen, extra="forbid" and strict. Validation runs through
model_validate_json rather than model_validate(dict) — a deliberate choice,
not an accident. In pydantic's strict JSON mode a list still becomes a set and
an ISO string still becomes a datetime, because JSON has no set or datetime
type to be strict about; but "5" is still not an int, 1.5 is still out of range
for a 0..1 multiplier, and an unknown key is still fatal. Strict dict-mode
would reject ["S1_FIXFADE"] for set[PodEnum] and make the contract literally
unsatisfiable from a language model. See parse_doctrine.

RISK MULTIPLIER ONLY EVER REDUCES. It is bounded [0.0, 1.0] and multiplies a
size computed elsewhere. 1.0 is the ceiling, not a target, and there is no
value it can take that increases exposure beyond what the sizer already
allowed.

WHAT THIS MODULE DOES NOT DO
Nothing consumes a doctrine yet. Pods arrive in Task 18 and the wiring in
Task 16/18. This file imports nothing from ai.analysis or risk/ — the client
seam, the fence-stripper and the prompt rendering are its own copies, so the
two rings can drift apart without breaking each other.
"""
import argparse
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Set

from pydantic import BaseModel, Field, model_validator

# CLI-only: `python3 -m ai.doctrine --once` is a standalone entry point, so
# .env must be loaded HERE, before `import config` below reads the environment.
# config.py reads env vars at module-import time, so loading it in main() would
# be far too late. A library import of this module (or the agent loop started
# by backend.py) does not hit this branch and inherits the parent environment.
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

import config
from core import database
from core.state import STATE

logger = logging.getLogger(__name__)

# Source values for the doctrines table. FABLE means the model answered and
# its answer survived validation; everything else is a substituted FLAT.
SOURCE_FABLE = "FABLE"
SOURCE_PARSE_FALLBACK = "PARSE_FALLBACK"
SOURCE_EXPIRY_FALLBACK = "EXPIRY_FALLBACK"

ACTIVE_SESSIONS = ("LONDON", "OVERLAP", "NY")

# Built from config so the pod roster has exactly one definition. Task 18 adds
# the pods themselves; this is only the vocabulary the doctrine may use.
PodEnum = Enum("PodEnum", {name: name for name in config.POD_NAMES}, type=str)

# State-vector dims deliberately WITHHELD from the doctrine prompt. The head of
# desk reasons about posture, not levels: price, session_high and session_low
# are the raw price field and it has no business quoting them back. Excluding
# them here is what makes "the doctrine prompt contains no prices" testable.
_PRICE_DIMS = ("price", "session_high", "session_low")


class Doctrine(BaseModel):
    """
    The only shape Ring 2 may emit. Frozen so a consumer cannot widen a posture
    it was handed and claim the strategist said so.
    """

    model_config = {"frozen": True, "extra": "forbid", "strict": True}

    ts: datetime
    regime: Optional[str] = None
    bias: Literal["LONG_ONLY", "SHORT_ONLY", "BOTH", "FLAT"]
    conviction: int = Field(ge=0, le=10)
    risk_multiplier: float = Field(ge=0.0, le=1.0)
    enabled_pods: Set[PodEnum] = Field(default_factory=set)
    swing_signals_allowed: bool
    no_trade_reason: Optional[str] = None
    review_horizon_min: int = Field(ge=15, le=120)

    @model_validator(mode="after")
    def _flat_means_flat(self):
        """
        A FLAT doctrine that leaves things switched on is malformed, not
        merely odd — it is the exact shape a confused model produces when it
        says "stand down" while forgetting to disarm anything. Rejecting it
        here routes it to FLAT_FALLBACK, which really is disarmed.
        """
        if self.bias != "FLAT":
            return self
        if not self.no_trade_reason:
            raise ValueError("bias=FLAT requires no_trade_reason")
        if self.enabled_pods:
            raise ValueError(f"bias=FLAT must enable no pods, got {sorted(self.enabled_pods)}")
        if self.swing_signals_allowed:
            raise ValueError("bias=FLAT must not allow swing signals")
        return self

    def expires_at(self) -> datetime:
        return self.ts + timedelta(minutes=self.review_horizon_min)


def FLAT_FALLBACK(reason: str, source: str, now_utc: Optional[datetime] = None) -> Doctrine:
    """
    The safe default, and the return value of EVERY failure path in this file.

    Named in caps because it is a constant posture rather than a computation:
    flat, zero conviction, zero risk, nothing enabled, reviewed again in 15
    minutes. `source` is not a Doctrine field (a doctrine describes a posture,
    not its own provenance) — it is carried here so the log line and the
    persisted row agree on who produced this.
    """
    logger.warning("doctrine: FLAT fallback [%s] %s", source, reason)
    return Doctrine(
        ts=now_utc or datetime.now(timezone.utc),
        regime=None,
        bias="FLAT",
        conviction=0,
        risk_multiplier=0.0,
        enabled_pods=set(),
        swing_signals_allowed=False,
        no_trade_reason=reason,
        review_horizon_min=15,
    )


# ==========================================================================
# parsing
# ==========================================================================


def _defence(text: str) -> str:
    """
    Strip a single leading ```/```json fence and its matching trailing fence.

    This is the ONLY repair performed on a model response. No brace balancing,
    no quote fixing, no "take the first JSON-looking substring" — the price
    resolver's precedent. A response that needs repairing is a response we do
    not understand, and guessing at it is how a bad posture gets adopted.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n")[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


def parse_doctrine(raw: str, now_utc: datetime) -> Doctrine:
    """
    Turn a model response into a Doctrine, or into FLAT. Never raises.

    The timestamp is injected, never read from the response: the AI does not
    get to set its own clock, because a model that back-dates its ts would
    hand itself an already-expired posture or an eternal one.
    """
    if not isinstance(raw, str) or not raw.strip():
        return FLAT_FALLBACK("empty model response", SOURCE_PARSE_FALLBACK, now_utc)

    defenced = _defence(raw)
    try:
        data = json.loads(defenced)
    except (ValueError, TypeError) as exc:
        return FLAT_FALLBACK(f"response is not JSON: {exc}", SOURCE_PARSE_FALLBACK, now_utc)

    if not isinstance(data, dict):
        return FLAT_FALLBACK(
            f"response JSON is {type(data).__name__}, expected object",
            SOURCE_PARSE_FALLBACK,
            now_utc,
        )

    # Injected, and injected LAST so a model-supplied "ts" cannot survive.
    data["ts"] = now_utc.isoformat()

    try:
        # JSON mode on purpose — see the module docstring. Strictness is
        # retained; only JSON's own representational limits are accommodated.
        return Doctrine.model_validate_json(json.dumps(data))
    except Exception as exc:
        return FLAT_FALLBACK(
            f"doctrine failed validation: {exc}", SOURCE_PARSE_FALLBACK, now_utc
        )


# ==========================================================================
# prompt
# ==========================================================================


def _render_dims(state: Dict[str, Any]) -> List[str]:
    """Every known dim except the raw price fields, as 'name: value' lines."""
    lines = []
    for key in sorted(state or {}):
        if key in _PRICE_DIMS:
            continue
        value = state[key]
        if value is None or isinstance(value, (dict, list, set, tuple)):
            continue
        lines.append(f"  {key}: {value}")
    return lines


def _render_pod_stats(pod_stats: Optional[Dict[str, Any]]) -> str:
    if not pod_stats:
        return "no pod history"
    lines = []
    for pod in config.POD_NAMES:
        stats = pod_stats.get(pod)
        lines.append(f"  {pod}: {stats}" if stats else f"  {pod}: no history")
    return "\n".join(lines)


# Task 23: the pod-stats provider hook.
#
# fusion/learning_loop.py computes per-pod performance, but ai/ must not import
# fusion/ to fetch it — the doctrine has to keep working with the learning loop
# absent or broken. So the dependency is inverted: backend.py installs a
# provider at boot, and this module only ever calls whatever it was handed.
#
# Nothing here fails loudly. A missing or raising provider yields None, which
# build_doctrine_prompt already renders as "no pod history" — the exact
# behaviour that shipped in Task 15.
_POD_STATS_PROVIDER = None


def set_pod_stats_provider(provider) -> None:
    """Install the callable that supplies per-pod stats. None clears it."""
    global _POD_STATS_PROVIDER
    _POD_STATS_PROVIDER = provider
    logger.info("doctrine: pod stats provider %s", "installed" if provider else "cleared")


def current_pod_stats() -> Optional[Dict[str, Any]]:
    """
    Ask the provider, guarded. Returns None when there is no provider or it
    fails — a broken learning loop must never stop the desk from having a view.
    """
    provider = _POD_STATS_PROVIDER
    if provider is None:
        return None
    try:
        return provider()
    except Exception:
        logger.warning("doctrine: pod stats provider raised; prompting without history",
                       exc_info=True)
        return None


def build_doctrine_prompt(state: Dict[str, Any], pod_stats: Optional[Dict[str, Any]] = None) -> str:
    """
    The head-of-desk brief. Deliberately contains no price levels and no order
    vocabulary — there is nothing here to anchor a model toward emitting one.
    """
    dims = _render_dims(state) or ["  (no sensor data available)"]
    regime = state.get("macro_regime") if isinstance(state, dict) else None

    return "\n".join(
        [
            "You are the head of desk for a gold (XAUUSD) trading system.",
            "You do NOT place trades. You set POSTURE for the next period, and",
            "the execution rings decide whether anything is worth doing under it.",
            "",
            "## MACRO REGIME",
            f"  {regime if regime else 'unknown'}",
            "",
            "## STATE",
            *dims,
            "",
            "## POD PERFORMANCE",
            _render_pod_stats(pod_stats),
            "",
            "## AVAILABLE PODS",
            "  " + ", ".join(config.POD_NAMES),
            "",
            "## OUTPUT CONTRACT",
            "Reply with ONLY a single JSON object. No prose, no markdown fences.",
            "EXACTLY these keys, nothing more:",
            '  regime: string or null (your read of the macro regime)',
            '  bias: one of "LONG_ONLY", "SHORT_ONLY", "BOTH", "FLAT"',
            "  conviction: integer 0-10",
            "  risk_multiplier: number 0.0-1.0",
            "  enabled_pods: array drawn ONLY from the pod names above (may be empty)",
            "  swing_signals_allowed: boolean",
            "  no_trade_reason: string or null (REQUIRED when bias is FLAT)",
            "  review_horizon_min: integer 15-120",
            "",
            "risk_multiplier can only REDUCE risk. It scales a size that has",
            "already been calculated and approved elsewhere. 1.0 is the ceiling,",
            "not a target; choose below 1.0 whenever conditions are less than ideal.",
            "",
            "If bias is FLAT you MUST supply no_trade_reason, enable no pods,",
            "and set swing_signals_allowed to false.",
            "",
            "Do not include a timestamp; one is assigned for you.",
            "Anything else is discarded and treated as a refusal to answer,",
            "which is read as FLAT.",
            "",
            "## EXAMPLE (an active posture)",
            json.dumps(
                {
                    "regime": "YIELDS_FALLING",
                    "bias": "LONG_ONLY",
                    "conviction": 7,
                    "risk_multiplier": 0.6,
                    "enabled_pods": [config.POD_NAMES[0]],
                    "swing_signals_allowed": True,
                    "no_trade_reason": None,
                    "review_horizon_min": 30,
                }
            ),
            "",
            "## EXAMPLE (standing down)",
            json.dumps(
                {
                    "regime": "STRESS",
                    "bias": "FLAT",
                    "conviction": 0,
                    "risk_multiplier": 0.0,
                    "enabled_pods": [],
                    "swing_signals_allowed": False,
                    "no_trade_reason": "macro stress with no clear direction",
                    "review_horizon_min": 15,
                }
            ),
        ]
    )


# ==========================================================================
# the model call (own seam — nothing imported from ai.analysis)
# ==========================================================================


def _get_client():
    """
    Construct the Anthropic client. Imported lazily so this module loads with
    no SDK and no key present, and so tests monkeypatch this one seam instead
    of the network. The key comes from the environment only.
    """
    import anthropic  # lazy: keeps import-time clean and test-friendly

    return anthropic.Anthropic(
        api_key=config.ANTHROPIC_API_KEY,
        timeout=config.DOCTRINE_TIMEOUT_SECONDS,
    )


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * config.ANALYSIS_COST_PER_MTOK_INPUT
        + output_tokens / 1_000_000 * config.ANALYSIS_COST_PER_MTOK_OUTPUT
    )


def _response_text(resp) -> Optional[str]:
    content = getattr(resp, "content", None)
    if not content:
        return None
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts) if parts else None


def _call_model(prompt: str, model: str, max_tokens: int, label: str) -> Optional[str]:
    """
    One guarded call. Returns the raw text or None; never raises (INVARIANT 6).
    Budget is checked BEFORE the call and accumulated after it.
    """
    if STATE.budget_spent_today > config.MAX_DAILY_COST:
        logger.error(
            "%s: daily budget cap reached (spent=$%.4f > cap=$%.4f); not calling",
            label, STATE.budget_spent_today, config.MAX_DAILY_COST,
        )
        return None

    try:
        client = _get_client()
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.error("%s: API call failed (%s)", label, exc, exc_info=True)
        return None

    usage = getattr(resp, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cost = _estimate_cost(input_tokens, output_tokens)
    STATE.budget_spent_today += cost
    logger.info(
        "%s: usage in=%d out=%d est_cost=$%.4f budget_spent=$%.4f",
        label, input_tokens, output_tokens, cost, STATE.budget_spent_today,
    )
    return _response_text(resp)


# ==========================================================================
# persistence
# ==========================================================================


def _link_state_vector(conn, ts: datetime) -> Optional[int]:
    """Newest state vector at or before `ts`, within the RAG link window."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM state_vectors "
                "WHERE ts <= %s AND ts >= %s - (%s * INTERVAL '1 minute') "
                "ORDER BY ts DESC LIMIT 1",
                (ts, ts, config.RAG_LINK_WINDOW_MINUTES),
            )
            row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception:
        logger.warning("doctrine: state vector link failed; storing unlinked", exc_info=True)
        return None


def persist_doctrine(
    doctrine: Doctrine, source: str, raw_response: Optional[str] = None
) -> Optional[int]:
    """
    Append the doctrine to the audit table. Best-effort (INVARIANT 6): the
    posture is already decided in memory, so a dead database costs the record,
    never the decision. Returns the new row id, or None.
    """
    try:
        with database.get_conn() as conn:
            state_vector_id = _link_state_vector(conn, doctrine.ts)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO doctrines (ts, regime, bias, conviction, risk_multiplier, "
                    "enabled_pods, swing_signals_allowed, no_trade_reason, review_horizon_min, "
                    "state_vector_id, source, raw_response) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (
                        doctrine.ts,
                        doctrine.regime,
                        doctrine.bias,
                        doctrine.conviction,
                        doctrine.risk_multiplier,
                        sorted(pod.value for pod in doctrine.enabled_pods),
                        doctrine.swing_signals_allowed,
                        doctrine.no_trade_reason,
                        doctrine.review_horizon_min,
                        state_vector_id,
                        source,
                        raw_response,
                    ),
                )
                return int(cur.fetchone()[0])
    except Exception:
        logger.warning("doctrine: persist failed; posture stands unrecorded", exc_info=True)
        return None


# ==========================================================================
# issuing
# ==========================================================================


def issue_doctrine(
    state: Dict[str, Any],
    now_utc: datetime,
    pod_stats: Optional[Dict[str, Any]] = None,
) -> Doctrine:
    """
    Ask the head of desk for a posture, cage the answer, record it, return it.
    A failed call is not an error condition to propagate — it is a FLAT
    doctrine, persisted as such so the silence is visible later.
    """
    prompt = build_doctrine_prompt(state, pod_stats)
    raw = _call_model(prompt, config.DOCTRINE_MODEL, config.DOCTRINE_MAX_TOKENS, "doctrine")

    if raw is None:
        doctrine = FLAT_FALLBACK("model call failed or budget exhausted", SOURCE_PARSE_FALLBACK, now_utc)
        persist_doctrine(doctrine, SOURCE_PARSE_FALLBACK, raw_response=None)
        return doctrine

    doctrine = parse_doctrine(raw, now_utc)
    # A FLAT doctrine carrying a no_trade_reason the model never wrote is a
    # fallback; anything else validated as issued.
    source = SOURCE_FABLE if doctrine.bias != "FLAT" else _classify_flat(raw, doctrine)
    persist_doctrine(doctrine, source, raw_response=raw)
    logger.info(
        "doctrine: issued bias=%s conviction=%s risk_mult=%s pods=%s swing=%s source=%s",
        doctrine.bias, doctrine.conviction, doctrine.risk_multiplier,
        sorted(p.value for p in doctrine.enabled_pods), doctrine.swing_signals_allowed, source,
    )
    return doctrine


def _classify_flat(raw: str, doctrine: Doctrine) -> str:
    """
    Distinguish "Fable deliberately stood the desk down" from "we could not
    understand Fable". Both are FLAT; only one is the model's own judgement,
    and conflating them would hide a broken prompt behind a calm-looking row.

    The test is the WHOLE contract, not the bias field alone: the raw response
    must validate as a Doctrine in its own right AND come out FLAT. A response
    claiming bias=FLAT while enabling pods, allowing swing signals, omitting
    no_trade_reason or carrying an extra key did not stand us down — we
    rejected it and substituted our own posture. Crediting that to FABLE would
    record a model failure as a model decision, which is exactly the blindness
    this column exists to prevent.

    The re-parse is deterministic: the adopted doctrine's ts is the same value
    parse_doctrine injected, so this reproduces that parse rather than a new
    one, and cannot disagree with it.
    """
    try:
        data = json.loads(_defence(raw))
        if not isinstance(data, dict):
            return SOURCE_PARSE_FALLBACK
        data["ts"] = doctrine.ts.isoformat()
        revalidated = Doctrine.model_validate_json(json.dumps(data))
    except Exception:
        # Did not validate -> the FLAT we adopted is ours, not Fable's.
        return SOURCE_PARSE_FALLBACK

    return SOURCE_FABLE if revalidated.bias == "FLAT" else SOURCE_PARSE_FALLBACK


# ==========================================================================
# holder + expiry watchdog
# ==========================================================================


class DoctrineHolder:
    """
    Holds the current posture and enforces its expiry.

    A doctrine is only valid for review_horizon_min. Past that the held posture
    is not "probably still fine" — it is unreviewed, and an unreviewed posture
    is indistinguishable from no posture. current() degrades to FLAT rather
    than serving a stale opinion.
    """

    def __init__(self, persist: bool = True) -> None:
        self._lock = threading.Lock()
        self._current: Optional[Doctrine] = None
        self._announced = False  # log + persist the fallback once, not per call
        self._persist = persist

    def set(self, doctrine: Doctrine) -> None:
        with self._lock:
            self._current = doctrine
            self._announced = False

    def get_held(self) -> Optional[Doctrine]:
        with self._lock:
            return self._current

    def current(self, now_utc: datetime) -> Doctrine:
        with self._lock:
            held = self._current
            if held is not None and now_utc <= held.expires_at():
                return held

            reason = (
                "no doctrine has been issued"
                if held is None
                else f"doctrine expired at {held.expires_at().isoformat()}"
            )
            first_time = not self._announced
            self._announced = True

        # Outside the lock: persisting must never hold up another thread.
        if first_time:
            logger.error("doctrine: DOCTRINE_EXPIRED — %s; degrading to FLAT", reason)
            fallback = FLAT_FALLBACK(reason, SOURCE_EXPIRY_FALLBACK, now_utc)
            if self._persist:
                persist_doctrine(fallback, SOURCE_EXPIRY_FALLBACK)
            return fallback
        return FLAT_FALLBACK(reason, SOURCE_EXPIRY_FALLBACK, now_utc)


HOLDER = DoctrineHolder()


# ==========================================================================
# triage
# ==========================================================================


def build_triage_prompt(state_delta: Dict[str, Any]) -> str:
    lines = [f"  {k}: {v}" for k, v in sorted((state_delta or {}).items()) if k not in _PRICE_DIMS]
    return "\n".join(
        [
            "A gold trading system holds a strategic posture that is expensive to revisit.",
            "Below is what has CHANGED in its market state since the posture was set.",
            "",
            "## DELTA",
            *(lines or ["  (nothing changed)"]),
            "",
            "Score 0.0-1.0 how strongly this warrants waking the head of desk to",
            "reconsider the posture. 0.0 = noise, 1.0 = the picture has materially changed.",
            "Reply with ONLY a number between 0 and 1.",
        ]
    )


def triage(state_delta: Dict[str, Any], now_utc: datetime) -> float:
    """
    Cheap gate in front of an expensive model. Returns 0.0-1.0.

    Fails toward 1.0 — waking the head of desk. A triage that cannot be read is
    an unknown, and the safe response to an unknown here is to escalate, not to
    stay quiet: the cost of a needless Fable call is cents, the cost of missing
    a regime break is the account.
    """
    raw = _call_model(
        build_triage_prompt(state_delta), config.TRIAGE_MODEL, config.TRIAGE_MAX_TOKENS, "triage"
    )
    if raw is None:
        logger.warning("triage: call failed; escalating (score=1.0)")
        return 1.0

    try:
        score = float(_defence(raw).strip().split()[0].rstrip(","))
    except (ValueError, IndexError, AttributeError):
        logger.warning("triage: unparseable score %r; escalating (score=1.0)", raw[:120])
        return 1.0

    if score != score or not (0.0 <= score <= 1.0):
        logger.warning("triage: score %r out of range; escalating (score=1.0)", score)
        return 1.0
    return score


# ==========================================================================
# agent
# ==========================================================================


def cadence_minutes(state: Dict[str, Any]) -> int:
    """Active sessions justify a 15-minute posture review; quiet ones do not."""
    session = (state or {}).get("session_label")
    return (
        config.DOCTRINE_CADENCE_ACTIVE_MIN
        if isinstance(session, str) and session.upper() in ACTIVE_SESSIONS
        else config.DOCTRINE_CADENCE_QUIET_MIN
    )


def state_delta(current: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Dims whose value changed since the last issued doctrine."""
    if not previous:
        return dict(current or {})
    return {
        key: value
        for key, value in (current or {}).items()
        if previous.get(key) != value
    }


def run_doctrine_agent() -> None:
    """
    Wake on cadence, triage the delta, and re-issue when it matters.

    Survival contract: any exception in a cycle is logged and swallowed, and
    the loop continues (INVARIANT 6). backend.py registers this as kind="loop"
    in Task 16 — this module does not touch the registry.
    """
    logger.info(
        "doctrine agent starting: cadence active=%smin quiet=%smin, triage threshold=%s",
        config.DOCTRINE_CADENCE_ACTIVE_MIN,
        config.DOCTRINE_CADENCE_QUIET_MIN,
        config.TRIAGE_THRESHOLD,
    )
    last_issued_at: Optional[datetime] = None
    last_issued_state: Optional[Dict[str, Any]] = None

    while True:
        sleep_minutes = config.DOCTRINE_CADENCE_QUIET_MIN
        try:
            now = datetime.now(timezone.utc)
            state = dict(STATE.market_data)
            sleep_minutes = cadence_minutes(state)

            elapsed = (
                None if last_issued_at is None
                else (now - last_issued_at).total_seconds() / 60.0
            )
            cadence_due = elapsed is None or elapsed >= sleep_minutes

            score = 0.0
            if not cadence_due:
                score = triage(state_delta(state, last_issued_state), now)

            if cadence_due or score >= config.TRIAGE_THRESHOLD:
                # Task 23: the ONE modified line. current_pod_stats() is
                # guarded and returns None when no provider is installed, so
                # this is identical to the previous call until backend.py
                # wires the learning loop in.
                doctrine = issue_doctrine(state, now, current_pod_stats())
                HOLDER.set(doctrine)
                last_issued_at = now
                last_issued_state = state
            else:
                logger.info("doctrine: triage %.2f below threshold; holding posture", score)
        except Exception:
            logger.exception("doctrine agent: cycle failed; continuing")

        time.sleep(max(1, int(sleep_minutes)) * 60)


# ==========================================================================
# CLI
# ==========================================================================


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="NEXUS doctrine (Ring 2 output cage)")
    parser.add_argument(
        "--once", action="store_true", help="Issue exactly one doctrine and print it"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )

    if not args.once:
        parser.error("nothing to do; pass --once")

    now = datetime.now(timezone.utc)
    state = dict(STATE.market_data)
    if not state:
        logger.warning(
            "doctrine --once: AppState is empty in a fresh process (no sensors have "
            "run here); the prompt will carry no dims"
        )

    doctrine = issue_doctrine(state, now)
    print()
    print(json.dumps(json.loads(doctrine.model_dump_json()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
