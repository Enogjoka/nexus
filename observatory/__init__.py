"""
NEXUS Observatory — a separate, read-only process that serves live NEXUS data
to the app (plan Appendix B).

Hard rules, each enforced by tests/test_observatory.py:
  * This package imports nothing from the trading repo. Trading modules have
    import-time side effects (risk.stage records a BOOT row on import); the
    observatory must be unable to trigger any of them.
  * It connects only as the read-only `observatory` role, and every session is
    read-only with a 5 s statement timeout and a 5 s connect timeout.
  * It contains no SQL that can write: every statement is a bounded SELECT.
"""
