# NEXUS — Working Rules

- ONE task per session. Touch ONLY files listed in the task brief.
- Acceptance test must pass; paste proof; commit "task N: <name>" on
  branch task/N-*. No merges — a human merges after architect review.

## INVARIANTS

1. STAGE read once at boot, no runtime mutation path may exist.
2. AI never emits raw prices or raw orders — pydantic contracts only.
3. risk/kernel.py (future) imports nothing from ai/.
4. Never weaken validator gates, sizing, or pydantic strictness.
5. Every order path must call kernel.permit() (future).
6. Every external call has timeout + try/except + logged failure.
7. Config knobs in config.py, secrets in env only, nothing in DB, nothing in git.

## UNTOUCHABLES (grows over time)

- config.py stage mechanism
- this file
- ai/price_resolver.py (anchor-enum contract; the AI/price wall)
- risk/validator.py (7-rule pre-flight gate; only restricts, never loosens)
- risk/sizing.py (pure position sizer; never rounds up, never exceeds risk budget)
- risk/stage.py (stage ladder + demotion flag; all decisions import-time, no setter)

## If the spec conflicts with reality

STOP, report, do not improvise.
