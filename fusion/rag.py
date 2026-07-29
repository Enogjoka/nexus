"""
NEXUS RAG memory — recall similar past world-states and what actually happened
in them, with a confidence bound honest about small samples.

The embedding is computed LOCALLY and deterministically from the state-vector
dims: no external embedding API, no pgvector. At this corpus size a JSONB
column plus a numpy cosine sweep is not merely sufficient, it is preferable —
it keeps the vector inspectable, reproducible, and free.

WHAT IS DELIBERATELY NOT INCLUDED in the vector:
  * `price`, `session_high`, `session_low` — absolute levels. Gold at 2000 and
    gold at 4000 can be the *same* market state; embedding the level would
    make every old regime look dissimilar for the wrong reason.
  * `cot_mm_net` — the raw contract count. Its percentile (`cot_mm_net_pctile`)
    is the comparable-across-time form, and that IS included.

Missing data: each numeric dim contributes TWO dims — the normalized value
(0.5 when absent, i.e. neutral) and a presence mask (1.0/0.0). Without the
mask, "sensor down" and "sensor reading exactly mid-range" would embed
identically. Categorical dims are one-hot and need no mask: an all-zero block
already encodes absence unambiguously.

WIRING DEFERRAL (stated deliberately): signals gain `state_vector_id` at
persist time, but ai/analysis.py — which owns signal persistence — is NOT in
this task's file list. So nothing calls `link_latest()` automatically yet:
Task 12 wires the nightly job, and a later analyst enrichment calls `recall()`
live. What THIS task delivers is the machinery plus a backfill path:
`python3 -m fusion.rag --backfill` embeds every state_vectors row and links
any unlinked signal to the state vector it was born into.
"""
import argparse
import json
import logging
import math
from datetime import timedelta
from typing import Any, Dict, List, Optional

import numpy as np

# CLI-only: `python3 -m fusion.rag --backfill` is a standalone entry point, so
# .env must be loaded here, BEFORE `import config` below reads the environment
# (config.py reads env vars at module-import time). A library import of this
# module does NOT hit this branch -- mirrors the sensors.
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

import config
from core import database

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = ("STOPPED", "TP2", "BE")


def _build_dim_order() -> List[str]:
    """Expand the config ranges/categoricals into the concrete, ordered dim
    names. Order is stable as long as the config dicts are (they are ordinary
    dicts, insertion-ordered), which is what keeps old embeddings comparable."""
    dims: List[str] = []
    for field in config.RAG_DIM_RANGES:
        dims.append(field)
        dims.append(f"{field}__present")
    for field, values in config.RAG_CATEGORICAL_VALUES.items():
        for value in values:
            dims.append(f"{field}__{value}")
    return dims


# The authoritative dim order of every vector this module produces.
RAG_DIM_ORDER: List[str] = _build_dim_order()
RAG_DIM_COUNT: int = len(RAG_DIM_ORDER)


def _num(value) -> Optional[float]:
    """A real finite float, or None. Bools/strings/NaN/inf are 'missing'."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def vectorize(sv_row: Dict[str, Any]) -> List[float]:
    """
    Turn a state_vectors row (dict) into the deterministic feature vector
    described in RAG_DIM_ORDER. Pure: same input always yields the same output,
    with no clock, network, or database involved.
    """
    vector: List[float] = []

    for field, (low, high) in config.RAG_DIM_RANGES.items():
        raw = sv_row.get(field)
        # BOOLEAN columns arrive as real bools; they are legitimate 0/1 values
        # here rather than "missing", so convert before the finiteness check.
        if isinstance(raw, bool):
            raw = 1.0 if raw else 0.0
        value = _num(raw)
        if value is None:
            vector.extend([0.5, 0.0])  # neutral + "absent"
            continue
        span = high - low
        normalized = 0.5 if span == 0 else (value - low) / span
        vector.extend([min(1.0, max(0.0, normalized)), 1.0])

    for field, values in config.RAG_CATEGORICAL_VALUES.items():
        actual = sv_row.get(field)
        vector.extend([1.0 if actual == value else 0.0 for value in values])

    return vector


def cosine_similarity(a, b) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is a zero
    vector (undefined direction) or the lengths disagree."""
    va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if va.shape != vb.shape or va.size == 0:
        return 0.0
    norm = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if norm == 0.0:
        return 0.0
    return float(np.dot(va, vb) / norm)


def embed_and_store(conn, sv_id: int) -> Optional[List[float]]:
    """
    Vectorize one state_vectors row and write its embedding JSONB. Returns the
    vector, or None if the row does not exist.
    """
    columns = list(config.RAG_DIM_RANGES) + list(config.RAG_CATEGORICAL_VALUES)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(columns)} FROM state_vectors WHERE id = %s", (sv_id,)
        )
        row = cur.fetchone()
        if row is None:
            logger.warning("embed_and_store: state_vectors id=%s not found", sv_id)
            return None

        sv_row = dict(zip(columns, row))
        vector = vectorize(sv_row)
        cur.execute(
            "UPDATE state_vectors SET embedding = %s::jsonb WHERE id = %s",
            (json.dumps(vector), sv_id),
        )
    return vector


def recall(conn, current_vector: List[float], k: int = None) -> List[Dict[str, Any]]:
    """
    Find the k most similar past world-states that actually produced a
    RESOLVED signal, newest-similarity first.

    Only state vectors back-linked to a signal with a terminal outcome
    (STOPPED / TP2 / BE) are candidates — an un-traded or still-open state
    teaches nothing about outcomes.

    Thin-recall guard: if fewer than config.RAG_MIN_SAMPLES precedents clear
    config.RAG_MIN_SIM, this returns [] rather than a confident-looking answer
    built from one or two coincidences.
    """
    k = config.RAG_K if k is None else k
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sv.id, sv.embedding, s.outcome_r, s.grade, s.direction, s.ts "
            "FROM state_vectors sv "
            "JOIN signals s ON s.state_vector_id = sv.id "
            "WHERE sv.embedding IS NOT NULL AND s.outcome_r IS NOT NULL "
            "AND s.status IN %s",
            (_TERMINAL_STATUSES,),
        )
        rows = cur.fetchall()

    scored: List[Dict[str, Any]] = []
    for sv_id, embedding, outcome_r, grade, direction, signal_ts in rows:
        if not isinstance(embedding, list):
            continue
        similarity = cosine_similarity(current_vector, embedding)
        if similarity < config.RAG_MIN_SIM:
            continue
        scored.append(
            {
                "state_vector_id": sv_id,
                "similarity": similarity,
                "outcome_r": float(outcome_r),
                "grade": grade,
                "direction": direction,
                "signal_ts": signal_ts,
            }
        )

    if len(scored) < config.RAG_MIN_SAMPLES:
        logger.info(
            "recall: only %d precedent(s) above similarity %.2f (need %d); returning none",
            len(scored),
            config.RAG_MIN_SIM,
            config.RAG_MIN_SAMPLES,
        )
        return []

    scored.sort(key=lambda item: item["similarity"], reverse=True)
    return scored[:k]


def wilson_stats(recalls: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """
    Win rate over the recalled precedents plus its Wilson score LOWER bound at
    config.RAG_WILSON_Z (95%). The lower bound is the number that matters: it
    is what stops "3 wins from 4" being read as a 75% edge.

    Returns None for an empty recall set (nothing to summarize).
    """
    if not recalls:
        return None

    n = len(recalls)
    wins = sum(1 for item in recalls if item["outcome_r"] > 0)
    p = wins / n
    z = config.RAG_WILSON_Z

    denominator = 1.0 + (z * z) / n
    center = (p + (z * z) / (2 * n)) / denominator
    margin = (z * math.sqrt(p * (1 - p) / n + (z * z) / (4 * n * n))) / denominator

    return {
        "n": n,
        "wins": wins,
        "win_rate": p,
        "wilson_lower": max(0.0, center - margin),
        "avg_r": sum(item["outcome_r"] for item in recalls) / n,
    }


def link_latest(conn, signal_id: int) -> Optional[int]:
    """
    Back-link a signal to the newest state vector that precedes it by no more
    than config.RAG_LINK_WINDOW_MINUTES. Returns the linked state_vectors id,
    or None when the signal has no timestamp or nothing sits in the window
    (a signal born with no recorded world-state is left unlinked rather than
    attached to a stale one).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT ts FROM signals WHERE id = %s", (signal_id,))
        row = cur.fetchone()
        if row is None or row[0] is None:
            logger.warning("link_latest: signal id=%s not found or has no ts", signal_id)
            return None
        signal_ts = row[0]
        window_start = signal_ts - timedelta(minutes=config.RAG_LINK_WINDOW_MINUTES)

        cur.execute(
            "SELECT id FROM state_vectors WHERE ts <= %s AND ts >= %s ORDER BY ts DESC LIMIT 1",
            (signal_ts, window_start),
        )
        candidate = cur.fetchone()
        if candidate is None:
            logger.info(
                "link_latest: no state vector within %s min of signal id=%s; leaving unlinked",
                config.RAG_LINK_WINDOW_MINUTES,
                signal_id,
            )
            return None

        cur.execute(
            "UPDATE signals SET state_vector_id = %s WHERE id = %s", (candidate[0], signal_id)
        )
    return candidate[0]


def backfill(conn) -> Dict[str, int]:
    """
    Embed every state_vectors row that lacks an embedding and link every
    signal that lacks a state_vector_id. Idempotent: a second run finds
    nothing left to do and reports zeros.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM state_vectors WHERE embedding IS NULL ORDER BY id")
        pending = [row[0] for row in cur.fetchall()]
    embedded = sum(1 for sv_id in pending if embed_and_store(conn, sv_id) is not None)

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM signals WHERE state_vector_id IS NULL ORDER BY id")
        unlinked = [row[0] for row in cur.fetchall()]
    linked = sum(1 for signal_id in unlinked if link_latest(conn, signal_id) is not None)

    return {
        "embedded": embedded,
        "linked": linked,
        "unlinkable": len(unlinked) - linked,
    }


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="NEXUS RAG memory over state vectors")
    parser.add_argument(
        "--backfill", action="store_true",
        help="Embed all un-embedded state vectors and link unlinked signals, then exit",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.backfill:
        with database.get_conn() as conn:
            summary = backfill(conn)
        summary["dims"] = RAG_DIM_COUNT
        print(summary)
        return

    parser.error("nothing to do; pass --backfill")


if __name__ == "__main__":
    main()
