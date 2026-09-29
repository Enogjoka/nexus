# NEXUS — Improvement Plan (Phase 1: Audit) — VERIFIED

**Target path in repo:** `docs/architect/NEXUS_IMPROVEMENT_PLAN_verified.md` (the unverified draft is `docs/architect/NEXUS_IMPROVEMENT_PLAN.md`)
**Author:** Fable, Head Architect (draft 2026-09-23) · **Verified:** 2026-09-29, Claude Code session on branch `architect/phase1-verify` (read-only) · **Status:** Phase 1 verification complete. Phase 2 is gated on human approval.
**Stage at time of writing:** PAPER · **Operator model:** solo

> **What changed in this version.** Every finding in §4 now carries a **Status** line (CONFIRMED / PARTIAL / REFUTED) with file:line, SQL or journal evidence. §3 is rebuilt from SQL and replaces the Telegram tally. §10–§15 are new: verification method, verification table, the V-0 answer, new findings F-35 to F-45, the test-residue census and app readiness. Recommendations in §5–§9 and the appendices are the 2026-09-23 draft, unchanged. Read them together with the statuses and new findings: where they conflict, the verified text wins.
>
> **Evidence sources for the verification:** (1) the repo at `main` @ `c08ed90`; (2) `nexus_prodcopy`, a read-only restore of the production DB taken **2026-09-29** (latest row: `doctrines` 2026-09-29 10:23:34 UTC). Every session was forced to `default_transaction_read_only=on` and timezone UTC, and no pytest run touched it; (3) the production journal `~/nexus_evidence/nexus_journal.txt`, 53,444 lines, 2026-09-20 09:38:00 → 2026-09-29 10:32:58 UTC. Journal line numbers below refer to that file. `dmesg` was not available, so Q9 relies on journal lines only.
>
> **Headline corrections to the draft:** (a) F-11's cause is not missing data. A `Decimal` type check silently discards every macro value read from Postgres. The same bug makes every RAG embedding numerically blank (**F-35**). (b) F-27 is **refuted**: both restarts (2026-09-23 06:42 and a second one on 2026-09-25 06:49 that the draft missed) were clean SIGTERM stops, not OOM kills. (c) Since **2026-09-25 12:06:30 UTC** every Anthropic call has failed with "credit balance is too low" (901 errors). The system has been AI-blind for 4 days while its heartbeat reported healthy (**F-36**). (d) The swing book's full Frankfurt record is **139 closed paper trades, −65.45R, from as few as 7 independent decisions**. That is not statistically meaningful.

> **How this audit was produced, and its limits.** This plan was written from the architect seat, not from a Claude Code session with live repo and database access. Its evidence base:
> 1. Line-by-line source audits performed during the build: `ai/price_resolver.py`, `risk/validator.py`, `risk/sizing.py`, `ai/analysis.py`, `ops/telegram_bot.py`, `risk/stage.py`, `risk/kernel.py` (plus the Task-18 amendment diff), `ai/doctrine.py`, `exec_/router.py`.
> 2. Every task report and fix pass: Tasks 0–24, 1.5, 12.5, 16.1, 22.1 and F1 (Frankfurt deploy kit).
> 3. The Frankfurt boot journal of 2026-09-20 and the preflight output.
> 4. The Telegram alert stream 2026-09-20 → 2026-09-23.
> 5. External research, verified 2026-09-23 (sources in §7).
>
> Every finding cites its evidence. Items tagged **[VERIFY-SQL Qn]** or **[VERIFY-JOURNAL]** must be confirmed with the read-only queries in Appendix A before any change is made. Telegram timestamps are the operator's local time (UTC+2); everything else here is UTC.

---

## 0. Stop-the-line: the mission prompt's premise does not match the system

The Head Architect mission prompt states that NEXUS "is finished and live with real money on EBC Financial Group MT5". **The evidence says it is not.**

| Claim in the prompt | Reality | Evidence |
|---|---|---|
| Live with real money | Stage **PAPER**, fill mode `MODELED` | Every `/status` reply 2026-09-20→23 shows `stage: PAPER`. Frankfurt boot journal: `kernel: constructed \| stage=PAPER fill_mode=MODELED … broker_reconcile=False` |
| MT5 execution on a Windows VPS | No Windows box exists. `BRIDGE_KIND="SIM"`. `RealMT5Bridge` has never executed | Task 16 report; Windows decision "B" (defer) recorded 2026-08-06 |
| EBC account connected | No broker credential exists anywhere (solo control #1: demo-only until the promotion pack passes) | Solo controls adopted at the Task 16 gate |
| Two-person team | Solo operator | Declared at the Task 16 gate |
| "Six validator gates" | Eight checks: RULE0 (entry drift) + RULE1–RULE7 | `risk/validator.py` source audit (Task 3) |
| "Linux brain machine" | DigitalOcean droplet `ubuntu-s-1vcpu-2gb-fra1-01` (Frankfurt): 1 vCPU, 2 GB RAM, **shared with other PM2/Node projects**, `sudo` broken (audit-plugin error) — administration via `su` | Migration checkpoints 1–3 |
| Required reading list | `CLAUDE.md` is in the repo. `ARCHITECTURE.md`, `NEXUS_TEAM_WORKFLOW.md`, `IMPLEMENTATION.md` and the v6/v7 design docs were never committed to the rebuilt repo | Repo history since Task 0 |
| Model strings `claude-fable-5-1` / `claude-opus-5` | Code uses `claude-fable-5` (analyst, doctrine) and `claude-sonnet-4-6` (triage). Current IDs: `claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5`, `claude-haiku-4-5-20251001` | §7 — Anthropic models overview, verified 2026-09-23 |

**V-0 — answer before anything else:** confirm that no broker credential (live *or* demo) has been entered on any machine. If one has, that is a CRITICAL incident — it bypassed the stage ladder, the 48-hour cooling-off and the promotion pack — and every other item in this plan waits behind it.

A corrected Claude Code prompt that reflects reality ships alongside this plan: `NEXUS_ARCHITECT_PROMPT_v2.md`.

---

## 1. Executive summary

> **Verification note (2026-09-29):** point 1's figures came from the Telegram tally for 09-20→23. SQL gives 79 closed, −49.35R for that window and **139 closed, −65.45R, 7–21 independent decisions** for the full Frankfurt run (§3). Point 3's regime cause is corrected by F-35. Add F-36: the AI has been offline since 2026-09-25 12:06 UTC (credits exhausted, no alert).

1. **The swing desk is ungoverned.** The doctrine does not bind the analyst (FLAT, expired and `swing_signals_allowed=False` are all ignored), the paper engine bypasses Ring 0, and one idea is cloned into as many as 25 simultaneous positions. On paper, 2026-09-20→23: **78 closed trades, ≈ −52R** — and a single 25-position clone batch accounts for −25R of it.
2. **The measurement layer cannot be trusted yet.** Production audit tables carry test residue (`stage_events` = 1,962 rows; polluted `doctrines` and `validator_log`), `/status` reads only the pod book (it reported $0.00 while 78 trades closed), and trade counts are inflated by clones (a handful of independent decisions, not 78). No promotion argument is valid until this is fixed.
3. **Two sensors feed the AI noise.** `news_heat` is saturated at ≈ 1.0 — and the model cites "max news heat" in most of its theses — while the regime HMM has never trained ("0 complete macro rows"), so validator RULE 5 is permanently inert.
4. **Ring 0 and the stage ladder are sound where they apply.** The kernel, stage governor, doctrine cage, resolver, validator, sizer and router all passed line-by-line audits. The defect is coverage — decision paths that never pass through them — not their logic.
5. **v8 should be smaller, not bigger:** one book and one order path through the kernel (paper included), doctrine binding every decision, one position per thesis, honest statistics (independence-adjusted, pre-registered, deflated), a no-AI baseline, and an Observatory that reads the audit trail that already exists instead of instrumenting locked files.

---

## 2. System as-built (ring by ring)

### 2.0 Deployment

| Item | As-built |
|---|---|
| Host | DigitalOcean droplet, Frankfurt, Ubuntu 24.04.4 LTS, 1 vCPU / 2 GB RAM, ~49% RAM used by other tenants before NEXUS; kernel reboot pending (6.8.0-71 running, -139 installed) |
| Runtime | Python 3.12.3 venv at `/home/nexus/nexus/venv` (inside the checkout); systemd `nexus.service`, `Restart=on-failure`, `StartLimitBurst=5` per 600 s |
| Database | PostgreSQL 16.15, local DB `nexus`, peer auth for OS user `nexus`; 16 migrations applied; corpus restored from `nexus_dev` on 2026-09-20 |
| Agents | 16 (10 loop, 6 subscriber) under `backend.py` supervisor |
| Stage / bridge | `STAGE=PAPER`, `BRIDGE_KIND=SIM` |
| Dev box | iMac, `nexus_dev` (the test suite runs only here) |
| Admin | `sudo` broken; root password SSH; administration via `su` |

### 2.1 Ring 3 — sensors

| Agent | Status | Evidence | Doc vs reality |
|---|---|---|---|
| `data/gold_agent.py` | LIVE — yfinance **`GC=F` (COMEX futures)** H1/H4/D1 every 300 s; indicators via `ta`; DXY; session label and high/low | Task 1/10 reports; Frankfurt journal | v6 said MT5 ticks for price. Reality: futures via Yahoo, not the spot instrument that would be traded (see F-13) |
| `sensors/fred.py` | LIVE — 6 series hourly | Task 7 acceptance (real yield 2.37 → 2.44) | As designed |
| `sensors/positioning.py` | COT LIVE (Disaggregated `72hh-3qpy`, pinned to "GOLD - COMMODITY EXCHANGE INC."). **COMEX stocks: fails every cycle** (ReadTimeout). GLD tonnage: `None` by design. GLD proxy: NaN close | Task 8 + fix 2; Frankfurt journal 2026-09-20 09:38:24 | COMEX coverage ratio never produced a value |
| `sensors/calendar_agent.py` | LIVE — ForexFactory weekly JSON, 4 h poll; arms RULE 1 | Task 9 | As designed |
| `sensors/news.py` | LIVE — 5 feeds (BLS 403, Kitco removed); **`news_heat` saturated** | Task 9 fix 2 (`news_heat` 0.99999); state vector `1.0000` | Crude by design; now a dead dimension (F-10) |
| `sensors/basis.py` | **Fails every cycle** — `XAUUSD=X` 404 (no Yahoo spot symbol exists) | Task 20; Frankfurt journal | S3 can never fire |
| `fusion/state_vector.py` | LIVE — one row per hour | Frankfurt journal | As designed |

### 2.2 Ring 2 — intelligence

| Component | Status | Evidence | Doc vs reality |
|---|---|---|---|
| `ai/analysis.py` (swing analyst) | LIVE — one Claude call per `market_update` once ≥ 30 min since last; model `claude-fable-5`; anchor → resolver → validator → sizer → `signals` | Task 5 audit; journal `SIGNAL_PERSISTED id=5314` | **Does not read the doctrine.** v7 says swing is gated by `swing_signals_allowed` (F-01) |
| `ai/doctrine.py` | LIVE — issues doctrines; FLAT-on-silence verified; **chronically expired** in quiet sessions | Task 15 audit; `/status` "EXPIRED 40m/49m/42m of 15m" | Cadence/horizon contradiction (F-06) |
| Triage | `claude-sonnet-4-6` (active; not the current Sonnet) | config | F-21 |
| `fusion/rag.py` | Embeds and links; **recall reaches no prompt** | Task 11/12 | Write-only memory (F-24) |
| `fusion/regime.py` | **Never trained** — "0 complete macro rows, need 168" | Frankfurt journal 2026-09-20 09:38:16 | RULE 5 permanently skipped (F-11) |
| `fusion/learning_loop.py` | LIVE — nightly and weekly; weekly Fable prose works ($0.0165/call) | Task 23; weekly 2026-08-18 | As designed |

### 2.3 Ring 1 — execution

| Component | Status | Evidence | Doc vs reality |
|---|---|---|---|
| `exec_/paper_engine.py` | LIVE — manages **swing** signals in `signals`: fill at H1 close crossing, intrabar exits, stop-first; **no kernel call; no concurrency cap**; `PAPER_TTL_HOURS=24` | Task 6; Telegram clone batches | Parallel book outside Ring 0 (F-02) |
| `exec_/pod_agent.py` | LIVE — S1/S2/S3 registered, S4 benched; enablement from doctrine (so mostly none) | Task 21; `/pods` all off | Pods have never traded live |
| `exec_/router.py` | LIVE — reserve-before-send, kernel between reserve and send; **only called by pod_agent** | Task 21 source audit | Swing does not use it until SHADOW (F-02) |
| `exec_/position_engine.py` | LIVE — manages router positions only | Task 21 | Second book (F-15) |
| `data/mt5_bridge.py` | `SimBridge` only (constant spread 0.35); `RealMT5Bridge` skeleton, never executed; no partial-close primitive | Task 16/21 | SHADOW blocker (F-30) |
| `link/` | Built and tested; unwired; `HMAC_SECRET` set on Frankfurt | Task 17; checkpoint 2 | Accept-time eviction DoS noted (F-31) |

### 2.4 Ring 0 — risk kernel and governance

| Component | Status | Evidence |
|---|---|---|
| `risk/kernel.py` | LIVE; watchdog registered; 15 breakers incl. `COST_GATE`, `POSITIONS_UNKNOWN`; fail-closed; **guards only the router path**; equity = SimBridge (`ACCOUNT_SIZE` + router P&L), so it is blind to the swing book | Task 14 + 18 audits |
| `risk/stage.py` | VERIFIED — import-time stage, file-flag demotion, AST self-audit | Task 13 audit |
| Kill switch | Drill executed 2026-08-07 on the iMac; **not repeated on Frankfurt**; expired under the promotion pack's 30-day rule | Task 22; Task 24 |
| Telegram deck | `/status /pods /doctrine /killswitch /clearkill /flatten`, two-step confirm, `command_audit` | Task 22 |

---

## 3. Performance evidence (rebuilt from SQL, 2026-09-29)

> **Small-sample honesty rule applies to everything in this section.** Under ~100 independent decisions nothing here is statistically meaningful. The numbers are presented because they expose *defects*, not because they measure an edge.

Source: `nexus_prodcopy.signals`, Frankfurt era = ids 5314–5495 (182 rows, persisted 2026-09-20 09:38:05 → 2026-09-25 11:53:08 UTC; no signal after that because API credits ran out, F-36). All rows: symbol `GC=F`, `execution_mode='PAPER'`. The 23 older rows (ids 7–1366, 2026-07-17 → 08-03, iMac era) are excluded. Script: read-only psycopg2 against `nexus_prodcopy`. Fill-time caveat: `filled_at` is the **open timestamp of the H1 bar** on which the fill was detected, not wall-clock (F-39).

### 3.1 Swing paper book — rows

| Metric | Frankfurt era (ids ≥ 5314) | Draft's Telegram window (closed before 2026-09-23 06:40) |
|---|---|---|
| Signals persisted | 182 | — |
| Filled (opened) | 139 | 94 (draft: ≈ 94 ✔) |
| Expired unfilled (TTL 24 h) | 43 | — |
| Closed with R | 139 | 79 (draft: 78) |
| STOPPED (−1.00R) | 114 | 66 (draft: 66 ✔) |
| BE after TP1 (+0.60/+0.75/+0.90) | 11 (1× 0.60, 9× 0.75, 1× 0.90) | 10 (draft: 10 ✔) |
| TP2 (+2.4 … +3.0) | 14 (10× 3.0, 1 each 2.4/2.5/2.6/2.8) | 3 (draft: 2) |
| **Net** | **−65.45R** | **−49.35R** (draft: ≈ −52.35R. The difference is one TP2 the Telegram tally missed) |
| Win rate, rows (R > 0) | 25/139 = **18.0%**, Wilson 95% [12.5%, 25.2%] | 13/79 |
| Expectancy, rows | −0.47R | −0.62R |
| Profit factor | 48.55R won / 114R lost = 0.43 | — |
| Longs | 35 closed, **0 wins**, −35.00R | 29 closed, all stopped, −29R (draft: 28) |
| Shorts | 104 closed, 25 wins, −30.45R | — |
| R by close date | 09-21 −29.75 · 09-22 −19.35 · 09-23 +19.55 · 09-25 −38.40 · 09-28 +2.50 | — |
| Max simultaneously open | **27** (kernel cap at PAPER: `max_concurrent=2`, journal line 5) | — |

### 3.2 Swing paper book — decisions (the numbers that matter)

Two independent clusterings of the 139 filled rows:

| Method | Decisions | Win rate (decision mean R > 0) | Wilson 95% | Σ decision-mean R | Long / Short |
|---|---|---|---|---|---|
| **A** — same fill bar (`filled_at`) + same direction | **21** | 6/21 = 28.6% | [13.8%, 50.0%] | −2.12R | 5 L (0 wins, −5.00) / 16 S (6 wins, +2.88) |
| **B** — same direction, entry within 1 × ATR(H1) of the cluster's first entry (ATR = `market_snapshot.1h.indicators.atr14` at persist) | **7** | 2/7 = 28.6% | [8.2%, 64.1%] | −4.36R | 2 L (0 wins, −2.00) / 5 S (2 wins, −2.36) |

Cluster sizes under B: 29, 29, 27, 23, 22, 8, 1. **Effective N is 7 to 21, not 139. Not statistically meaningful.** The Wilson interval under B spans 8%–64%.

Fill-bar groups (method A, UTC bar-open time → direction × rows → Σ R):

| Bar | Dir × n | Σ R | | Bar | Dir × n | Σ R |
|---|---|---|---|---|---|---|
| 09-21 00:00 | L×1 | −1.00 | | 09-22 19:00 | S×9 | +11.00 |
| **09-21 03:00** | **L×25** (entries 4408.85–4409.85) | **−25.00** | | 09-23 02:00 | L×7 | −7.00 |
| 09-21 12:00 | S×9 | +3.25 | | 09-23 05:00 | S×2 | +5.80 |
| 09-21 13:00 | L×1 | −1.00 | | 09-24 22:00 | S×8 | −8.00 |
| 09-21 23:00 | S×16 | −10.60 | | 09-24 23:00 | S×1 | −1.00 |
| 09-22 00:00 | S×2 | +5.40 | | 09-25 01:00 | S×9 | −9.00 |
| 09-22 01:00 | S×2 | +6.00 | | 09-25 03:00 | S×1 | −1.00 |
| 09-22 02:00 | L×1 | −1.00 | | 09-25 07:00 | S×4 | −4.00 |
| 09-22 07:00 | S×1 | −1.00 | | **09-25 09:00** | **S×19** (entries 4321.77–4332.27) | **−19.00** |
| 09-22 17:00 | S×7 | −7.00 | | 09-25 11:00 | S×3 | +6.10 |
| 09-22 18:00 | S×11 | −7.40 | | | | |

**Signal age at fill** (bar-open `filled_at` − persist `ts`, n = 139): median 5.3 h, p90 14.5 h, max 18.7 h; 83 fills older than 4 h, 27 older than 12 h. Ten fills show a *negative* age (min −1.13 h). That is the bar-stamp artefact (F-39), not time travel. Signal #5314: persisted 09:38:05, FILLED at 2026-09-21 03:11:26 (journal line 4001) → **17.6 h** wall-clock (draft ≈ 17.5 h ✔).

**Doctrine in force at each of the 182 persists** (latest doctrine with `ts ≤ signal.ts`): FLAT/FABLE live 98 · FLAT/EXPIRY_FALLBACK 61 (39 of them already past their own horizon) · BOTH/FABLE live 16 · FLAT/FABLE expired 7. **Zero Frankfurt doctrines ever set `swing_signals_allowed=true`.** Every one of the 182 signals was persisted against the doctrine (F-01).

The model's theses repeatedly cite "max news heat" (#5316, #5319, #5325, #5330, #5399, #5400, #5402, #5403 per the draft). `news_heat` is 1.00 in 317 of 340 state vectors (F-10).

**Would Ring 0 have prevented this?** Yes, had the swing book gone through it: `max_concurrent=2` against 27 open, and a −1.5% daily cap against −29.75R and −38.40R days.

### 3.3 Pods

`positions` and `fills` hold **0 rows** in the production copy (Q12). Pods have never traded live. Replay figures (Task 19/20) as in the draft: S1 3 trades/+$50.13, S2 18 trades/−$28.92 (cost drag 62.9%, upper bound), S3 0 (no spot feed), S4 refused (tick-native). Not statistically meaningful.

### 3.4 Gates and breakers (Q3, Q4)

- **Q3 as written is contaminated.** 688 `validator_log` rows are dated **2099-06-01 08:00 UTC**: 86 per rule × 8 (RULE0 PASS, RULE1 and RULE5 SKIPPED_NO_DATA, the rest PASS). A future-dated row falls inside every `ts > now() − N` window forever. That is the draft's "×86 while the system was dead" (F-38).
- **Frankfurt, real rows only** (2026-09-20 09:38 → now, 214 cycles × 8 = 1,712 rows): RULE0 PASS 187 / WAIT 27 · RULE1 PASS 208 / WAIT 6 · RULE2 PASS 214 · RULE3 PASS 212 / WARN 2 · RULE4 PASS 214 · **RULE5 SKIPPED_NO_DATA 214/214** (F-11/F-35) · RULE6 PASS 214 · RULE7 PASS 214. 181 of 214 cycles persisted a signal. The gates stopped 33 cycles, overwhelmingly on RULE0 entry drift.
- **Q4:** 0 rows. All 4,840 `kernel_events` rows are dated 2026-08-02 → 08-07 (test residue). Production has never called `permit()`.

### 3.5 Fable (Ring 2)

| Metric | Value | Source |
|---|---|---|
| Successful calls/day (analysis + doctrine) | 78 (09-20, partial), 111, 113, 112, 114, 50 (09-25 until 11:53) | journal `est_cost` lines, 578 total |
| Estimated spend/day (code's own prices) | $0.75, $1.08, $1.18, $1.12, $1.17, $0.56 | Σ `est_cost` per day. Real spend depends on list price (F-08, partial) |
| Frankfurt doctrines while credits lasted (09-20 09:38 → 09-25 12:06) | FABLE FLAT 281 · FABLE BOTH 49 · EXPIRY_FALLBACK 57 · PARSE_FALLBACK 1 · horizon 15–30 min | SQL |
| Share of minutes under a live FABLE doctrine | 67.7% (4,973 / 7,341) | SQL (F-06) |
| After 2026-09-25 12:06:30 | every call fails (400 "credit balance is too low", 901×); 258 PARSE_FALLBACK + 40 EXPIRY_FALLBACK doctrines, all FLAT | journal line 26332 onward; SQL (F-36, F-37) |
| Q5 (as written, 7-day window) | PARSE_FALLBACK FLAT 258 · FABLE FLAT 171 · EXPIRY_FALLBACK FLAT 74 · FABLE BOTH 25 · avg horizon 15 min | SQL |

### 3.6 Reliability

- Three boots on Frankfurt (`stage_events` ids 2099–2101): 2026-09-20 09:38:02, **2026-09-23 06:42:05**, **2026-09-25 06:49:47**. Both restarts were clean operator-initiated stops: `Stopping nexus.service` → `SIGTERM received — shutting down` → `Deactivated successfully` (journal 14949–14957, peak memory 232.5 MB; 25112–25120, peak 232.2 MB). No `Main process exited` or `Killed` lines exist. Not OOM (F-27 refuted). Who issued them is not in the unit journal (hypothesis: the operator. Confirm with `journalctl _COMM=systemctl` or `/var/log/auth.log`).
- **AI outage in progress:** no successful model call since 2026-09-25 11:53:26 UTC (F-36).
- August: the iMac soak died 2026-08-08 and went unnoticed for 42 days (draft; not re-verified here).

### 3.7 Execution quality

Not measurable: no broker, `fills` is empty. Spread constant 0.35, slippage fallback 0.10 (draft).

### 3.8 AI versus a no-AI baseline

Not measurable: no baseline exists (F-17, confirmed).

### 3.9 Market context (for interpretation, not attribution)

Unchanged from the draft. NEXUS's own H1 data shows gold falling from ≈ $4,409 (09-21) to ≈ $4,322–4,332 (09-25 fills). A falling tape punishes pullback longs, which fits 0-for-35 longs. With 2–5 long decisions, that is a narrative, not a finding.

---
## 4. Findings

Severity: **CRITICAL** = can lose money or leave the account unprotected at any stage above PAPER · **HIGH** = corrupts decisions or measurements, or blocks safe promotion · **MEDIUM** = weakens the system · **LOW** = hygiene. Effort: S (≤ 1 session), M (2–3), L (more). "Change risk" is the risk of making the fix, not of leaving it.

Files marked 🔒 are UNTOUCHABLE under `CLAUDE.md`; a fix there requires an explicit amendment brief with the additive-only grep proof (Task 18 precedent).

### CRITICAL

**F-01 · CRITICAL · Ring 2 → Ring 1 · The doctrine does not bind the swing path**
- **Status (verified 2026-09-29):** **CONFIRMED**. No doctrine read in `ai/analysis.py`. `HOLDER.current` is consumed only at `exec_/pod_agent.py:217` and `exec_/position_engine.py:435`. 0 Frankfurt doctrines allowed swing, yet 182 signals were persisted. (The draft's "~90 in 36 h" is actually 58 fills.)
- **Problem:** A FLAT or expired doctrine, `swing_signals_allowed=False`, `bias` and `risk_multiplier` have no effect on the swing analyst. The v7 contract ("doctrine can only restrict"; "silence means FLAT") holds for pods and fails for swing.
- **Evidence:** Journal 2026-09-20 09:38:11, doctrine issued with `swing=False`; 09:38:16, `run_analysis_cycle: outcome=SIGNAL_PERSISTED id=5314`. `/status` 2026-09-20 21:38 UTC shows "FLAT conviction 0/10 EXPIRED", yet ~90 swing positions opened over the next 36 hours. `ai/analysis.py` has no doctrine read (Task 5 source audit; Task 23 added only `macro_regime` and state-vector linking).
- **Recommendation:** `run_analysis_cycle` reads `HOLDER.current(now)` first. FLAT, expired or `swing_signals_allowed=False` → outcome `DOCTRINE_BLOCKED`, no Claude call (this also saves money), and a `validator_log` row `DOCTRINE_GATE/WAIT`. `LONG_ONLY`/`SHORT_ONLY` filters direction after parsing. `risk_multiplier` scales `risk_pct` passed to the sizer (`min(MAX_RISK_PCT, MAX_RISK_PCT × multiplier)` — it can only shrink).
- **Effort:** S · **Change risk:** Low — restriction only; `ai/analysis.py` is not locked; `ai/doctrine.py` is read, not edited.

**F-02 · CRITICAL · Ring 1 / Ring 0 · The swing book bypasses the kernel**
- **Status (verified 2026-09-29):** **CONFIRMED**. `exec_/paper_engine.py` has no permit/kernel/router reference. Q4 = 0. Peak 27 open against `max_concurrent=2` (journal line 5). Worst days −29.75R and −38.40R
- **Problem:** The paper engine fills and manages swing signals without calling `kernel.permit()`. The invariant "every order path consults `kernel.permit()`" is violated for the path that does all of the trading. The kernel's equity (SimBridge: `ACCOUNT_SIZE` + router P&L) never sees swing losses, so the daily-loss cap and the drawdown breaker are blind.
- **Evidence:** Task 6 design (paper engine predates Ring 0). Task 21 left swing on the paper engine "until the SHADOW cutover". Telegram: 25 simultaneous positions against a kernel cap of 2; a −28R day against a −1.5% daily cap that never fired.
- **Recommendation:** Route every swing signal through `router.submit(source="SWING")` at PAPER, with SimBridge filling it. The router docstring already states the principle — "the stage does not branch the code path" — and it must hold for swing too. The paper engine becomes a thin adapter or is retired; `position_engine` manages one book in `positions`. Then paper statistics describe the system that would actually trade.
- **Effort:** M · **Change risk:** Medium — it touches the order path. Mitigated: the router is audited, and reserve-before-send gives idempotency. Requires swing lifecycle tests (TP1 partial, BE, trail) on the router path, plus the partial-close primitive (F-30) in SimBridge.

**F-03 · CRITICAL · Ring 2 · One thesis becomes N positions**
- **Status (verified 2026-09-29):** **CONFIRMED**. Unconditional `INSERT … 'PENDING'` at `ai/analysis.py:421-440`. Every crossed PENDING fills on the same bar (`exec_/paper_engine.py:139-154`). 25 L at bar 09-21 03:00 (−25R); 19 S at 09-25 09:00 (−19R)
- **Problem:** Every analysis cycle (≥ 30 min) can persist a new PENDING signal at nearly the same anchor. PENDING signals live up to 24 hours. When price reaches the zone, all of them fill together and die together.
- **Evidence:** 25 LONG fills in one minute at 4409.35–4409.85 (2026-09-21 03:11 UTC), −25R. Later batches of 16, 11, 9, 9 and 7 shorts.
- **Recommendation:** Give every signal a `thesis_id` = hash(direction, entry anchor, price bucket of k × ATR(H1)). Before persisting: if an OPEN or PENDING signal exists with the same `thesis_id`, record a `REAFFIRMED` event and refresh its expiry instead of writing a row. Cap PENDING per direction at 1 (config). After F-02, the kernel's concurrency cap is the second line of defence.
- **Effort:** S · **Change risk:** Low.

### HIGH

**F-04 · HIGH · Ring 1 · Stale theses fill**
- **Status (verified 2026-09-29):** **CONFIRMED**. `config.py:107` TTL 24 h; `exec_/paper_engine.py:133-137`. #5314 filled after 17.6 h (journal 4001). Median age at fill 5.3 h; 27 fills older than 12 h
- **Problem:** Limit prices written for one market fill in another.
- **Evidence:** Signal #5314: persisted 09:38 UTC 2026-09-20, filled 03:11 UTC 2026-09-21 (≈ 17.5 h). `PAPER_TTL_HOURS=24`.
- **Recommendation:** TTL of 2–4 h (config). At fill time, re-run the drift check against live price and re-validate against current context; expire on failure with the reason logged.
- **Effort:** S · **Change risk:** Low.

**F-05 · HIGH · Data integrity · Production audit tables contain test residue**
- **Status (verified 2026-09-29):** **CONFIRMED**. 1,962/1,965 `stage_events`, 4,840/4,840 `kernel_events`, 688 `validator_log` rows dated 2099, test doctrines (§13)
- **Problem:** The corpus restored into production came from `nexus_dev`, which the test suite wrote to for months. The metrics that gate promotion are computed over polluted tables.
- **Evidence:** Preflight 2026-09-20: `stage_events=1962` (a handful of real boots exist). Task 24 promotion report: "boot record is FRAGMENTED". The 2026-08-18 weekly shows RULE0 PASS ×86 inside a window when the live system was dead. Doctrine tests persist `PARSE_FALLBACK` rows. The first Frankfurt signal received id 5314 although the table held 23 real signals — sequences consumed by thousands of test inserts.
- **Recommendation:** (a) `pg_dump` first. (b) Classify rows by provenance (test symbols such as `TST_*`, test ids, timestamps inside test-run windows, `2099` dates) and **move** them into `*_archive` tables. Never delete: the audit trail is evidence. (c) Add a guard in `tests/conftest.py`: refuse to run unless the database name is `nexus_dev` or ends in `_test`. (d) Re-run the promotion pack on clean data.
- **Effort:** M · **Change risk:** Low–medium (data operation; reversible from the dump).

**F-06 · HIGH · Ring 2 · The doctrine cadence contradicts its own horizon**
- **Status (verified 2026-09-29):** **CONFIRMED**. Cadence 15/60 min (`config.py:322-323`) with sleep independent of the 15–120 min horizon (`ai/doctrine.py:101,704-734`). A live FABLE doctrine covered 67.7% of minutes; 57 EXPIRY_FALLBACK
- **Problem:** Fable often issues a 15-minute horizon; in quiet sessions the agent sleeps 60 minutes. The doctrine expires, degrades to FLAT, and pods disarm — most of the day.
- **Evidence:** `/status` "age 40m of 15m EXPIRED", "49m of 15m", "49m of 15m", "42m of 15m" — every sample taken in the OFF/Asia hours or in the first minutes of London, i.e. while a 60-minute sleep chosen during a quiet session was still running.
- **Recommendation:** Sleep `min(cadence, horizon − 2 min)`. Re-issue before expiry, not after. Record expiries as their own metric. 🔒 `ai/doctrine.py` — amendment limited to `run_doctrine_agent`.
- **Effort:** S · **Change risk:** Low.

**F-07 · HIGH · Statistics · Trade counts are not independent**
- **Status (verified 2026-09-29):** **CONFIRMED**. 139 rows = 21 fill-bar decisions = 7 entry-zone decisions (§3.2)
- **Problem:** Clones inflate every count-based gate and make Wilson bounds falsely tight. `min_swing_signals` already read "PASS 23/20" in the Task 24 report while clones were in play.
- **Recommendation:** Promotion metrics count **decisions** (distinct `thesis_id`), report effective N next to row N, and compute win rate and expectancy per decision.
- **Effort:** S (after F-03) · **Change risk:** Low.

**F-08 · HIGH · Ring 2 · The budget guard under-counts real spend ≈ 3.3× and resets on restart**
- **Status (verified 2026-09-29):** **PARTIAL**. Constants 3.0/15.0 (`config.py:103-104`) reproduce the journal's `est_cost` exactly (line 64). The in-process counter resets on restart (`ai/doctrine.py:396-418`; `budget_spent=$0.0292` at 06:42:27 after the restart). The ×3.3 needs the real list price (not in repo or DB): hypothesis
- **Problem:** `ANALYSIS_COST_PER_MTOK_INPUT/OUTPUT = 3.0 / 15.0` are Sonnet-class numbers, but the calls go to `claude-fable-5`, listed at $10 / $50 per MTok. The $10/day cap therefore allows ≈ $33/day of real spend. The counter is in-process, so every restart zeroes it.
- **Evidence:** Journal: `usage in=1321 out=725 est_cost=$0.0148` → at list price ≈ $0.0495. Task 23 caveat "budget is tracked in-process".
- **Recommendation:** A per-model price table in config (with a verified-on date) and a durable `api_usage` table (ts, purpose, model, input, output, cache read/write, cost). The cap is enforced from the table's daily sum. Log the model per call. 🔒 `ai/doctrine.py` — amendment limited to the accounting inside `_call_model`.
- **Effort:** S · **Change risk:** Low.

**F-09 · HIGH · Ops · Production has no backups**
- **Status (verified 2026-09-29):** **PARTIAL**. The repo has only the one-off migration dump (`ops/deploy/RUNBOOK-FRANKFURT.md:149`) and no scheduled backup. Server cron and droplet backups are not observable: hypothesis
- **Problem:** The only copy of five months of corpus and audit trail lives on one shared droplet (plus a stale iMac copy).
- **Recommendation:** Nightly `pg_dump -Fc` → compress → off-box storage (DigitalOcean Spaces or Backblaze B2) with 14 dailies and 8 weeklies, a monthly restore test into a scratch DB, and a dead-man ping on success (F-12). Enable droplet backups as a second layer.
- **Effort:** S · **Change risk:** Low.

**F-10 · HIGH · Ring 3 · `news_heat` is saturated**
- **Status (verified 2026-09-29):** **CONFIRMED**. `news_heat` = 1.00 in 317/340 `state_vectors`, 0.94–0.99 in 20 more
- **Problem:** `tanh(count_6h / 10)` with 120+ prefiltered articles per 6 hours is ≈ 1.0 permanently. It carries no information, yet it feeds the analyst prompt, the doctrine prompt, the RAG vector and the regime HMM.
- **Evidence:** Task 9: `news_heat: 0.9999999999931506`. State vector: `1.0000`. Theses citing "max news heat" (§3.1).
- **Recommendation:** Replace it with `news_heat_z`: a z-score of the 6 h count against a trailing 30-day distribution of 6 h counts, clipped to [−3, 3]. Better, use GDELT DOC 2.0 `TimelineVol` for a gold query against a 4-week baseline (free, no key, 1 request per 5 s). Rename the field so no stale consumer silently reads the new scale.
- **Effort:** S · **Change risk:** Low (the RAG dim range and HMM feature must move with it).

**F-11 · HIGH · Ring 2 · The regime HMM has never trained; RULE 5 is permanently inert**
- **Status (verified 2026-09-29):** **PARTIAL**. Never trained: CONFIRMED (journal 69, 15041, 25184; nightly `retrain_regime: not_trained rows 0`). The draft's cause (NULLs/gaps) is **REFUTED** by Q7. The real cause is `Decimal` rejection (F-35)
- **Problem:** Training requires 168 hourly rows with every macro feature present. There are 0.
- **Evidence:** Frankfurt journal 2026-09-20 09:38:16: "0 complete macro rows, need 168" with 122+ vectors in the table. **[VERIFY-SQL Q7]** identifies which column is NULL (hypothesis: `dxy_change` needs consecutive hourly rows and the vector history has gaps).
- **Recommendation:** Macro drivers are daily data. Train the HMM on **daily** features backfilled from FRED (years of history are available in minutes) instead of waiting for hourly vectors. Classify hourly from the latest daily state. Keep the 168-row refusal as a data-quality gate for the hourly path.
- **Effort:** M · **Change risk:** Medium — RULE 5 starts firing; review its effect in shadow first.

**F-12 · HIGH · Ops · Silent death is only detected by the process that died**
- **Status (verified 2026-09-29):** **CONFIRMED**. Only an in-process heartbeat (`backend.py:220`). It logged `alive=10 … dead=0` on 2026-09-28 12:00:26 while every model call was failing (F-36)
- **Problem:** The August soak was dead for 42 days before anyone noticed. The 2026-09-23 restart was discovered from an uptime number.
- **Recommendation:** An external dead-man's switch: healthchecks.io (free tier, 20 checks, Telegram integration) pinged by the backend supervisor every heartbeat, alerting if silent for 3 minutes; a second check for the nightly backup; a third for the learning loop.
- **Effort:** S · **Change risk:** Low.

**F-13 · HIGH (SHADOW blocker) · Ring 3 · Prices are anchored to futures; execution will be spot**
- **Status (verified 2026-09-29):** **CONFIRMED**. `config.py:55` `YF_SYMBOL="GC=F"`; spot `XAUUSD=X` (`config.py:392`) returns no data (2,600 journal errors)
- **Problem:** Every anchor (EMAs, swing highs and lows, session high and low) is computed from `GC=F`. The instrument that will trade is XAUUSD spot at EBC. Front-month futures carry a carry premium over spot, and a continuous futures series jumps at contract rolls. At SHADOW, every entry and stop would be offset by the basis.
- **Recommendation:** At Windows bring-up, source bars, ticks, spread and anchors from the broker's MT5 feed for the traded symbol. Keep `GC=F` only as the futures leg of the basis sensor — which also revives S3.
- **Effort:** M · **Change risk:** Medium.

**F-14 · HIGH · Ops · Production runs a dependency set that was never tested**
- **Status (verified 2026-09-29):** **PARTIAL**. `requirements.txt`: 15 names, zero pins (CONFIRMED). Installed Frankfurt versions are not visible in journal or DB (hypothesis)
- **Problem:** `requirements.txt` is unpinned. Frankfurt installed `anthropic 1.7.0`, `pandas 3.0.6`, `numpy 2.5.3`, `scipy 1.18.1`, `yfinance 1.7.0`; the iMac suite passed on `anthropic 0.100.0`, `pandas 3.0.3`, `numpy 2.4.5`. The suite cannot run on production because it writes to the database.
- **Recommendation:** A lockfile (`uv lock` or `pip-compile`) committed to the repo; CI (GitHub Actions) runs the suite against a disposable Postgres service container on every PR; production installs from the lock only.
- **Effort:** S · **Change risk:** Low.

**F-15 · HIGH · Ops UI · `/status` reports one of two books**
- **Status (verified 2026-09-29):** **CONFIRMED**. `/status` reads only `positions` (`ops/telegram_bot.py:244,260,344`). `positions` = 0 rows while `signals` has 139 fills
- **Problem:** "open positions: 0 · closed today: 0 (net $+0.00)" while the swing book opened and closed dozens of positions.
- **Evidence:** Telegram 2026-09-21/22.
- **Recommendation:** Solved structurally by F-02 (one book). Until then, report both books, in R and in dollars.
- **Effort:** S · **Change risk:** Low.

### MEDIUM

**F-16 · MEDIUM · Ring 0 · No rollover, Friday-close or weekend protection**
- **Status (verified 2026-09-29):** **CONFIRMED**. End-of-week flat is pods-only (`exec_/position_engine.py:31,253`). There is no rollover or Friday logic in `risk/`
- **Problem:** Around the daily rollover (≈ 5 pm New York) spreads widen sharply and stops can fill badly; weekends gap. EBC also reduces leverage for new positions in windows before the daily close and around releases, and restricts new positions late on Friday. NEXUS blocks only the London fix and calendar events; end-of-week flat applies to pods only.
- **Recommendation:** Kernel breakers `ROLLOVER_BLACKOUT` (config window in broker server time) and `FRIDAY_CUTOFF`, plus a weekend policy for swing (tighten to breakeven or flatten). 🔒 Kernel amendment, additive only.
- **Effort:** S · **Change risk:** Low.

**F-17 · MEDIUM · Evaluation · There is no no-AI baseline**
- **Status (verified 2026-09-29):** **CONFIRMED**. No baseline strategy anywhere. "baseline" appears only as equity baselines in `risk/kernel.py:182,344`
- **Problem:** Without one, "the AI adds value" cannot be tested. Recent live benchmarks of LLM trading agents found most agents posting poor returns with weak risk management, most failing to beat buy-and-hold, and returns largely explained by passive market and style exposure (§7).
- **Recommendation:** Run shadow baselines through SimBridge with identical costs, sizing and stop/target geometry: (a) random entry, (b) a deterministic H4-trend/H1-pullback rule with no LLM, (c) pods without doctrine. Report the analyst's expectancy **minus** each baseline's, per decision.
- **Effort:** M · **Change risk:** Low (shadow only).

**F-18 · MEDIUM · Evaluation · The LLM layer must be evaluated forward-only**
- **Status (verified 2026-09-29):** **CONFIRMED** (policy gap). `tests/replay.py` replays pods only. No rule forbids replaying the LLM, and none is codified in the promotion pack
- **Problem:** Replaying the analyst over history before the model's knowledge cutoff (June 2026 for the Fable 5.x family) risks the model "remembering" prices. Any such backtest is contaminated.
- **Recommendation:** Codify it in the promotion pack: LLM-driven strategies are scored only on decisions made after deployment. Deterministic pods may be replayed.
- **Effort:** S · **Change risk:** None.

**F-19 · MEDIUM · Ring 1 · Doctrine fields without consumers**
- **Status (verified 2026-09-29):** **CONFIRMED**. `risk_multiplier` and `swing_signals_allowed` are read only by `ai/doctrine.py` and displayed at `ops/telegram_bot.py:379,381`. Pods use fixed `S1–S4_LOTS=0.01` (`config.py:377,384,404,414`)
- **Problem:** `risk_multiplier` is ignored everywhere (pods trade fixed `S?_LOTS`); `bias` is ignored on the swing path (fixed by F-01).
- **Recommendation:** Pod lots × `risk_multiplier`, floored to 0 below `MIN_LOT` — never rounded up.
- **Effort:** S · **Change risk:** Low.

**F-20 · MEDIUM · Ring 2 · Single-model dependency**
- **Status (verified 2026-09-29):** **CONFIRMED**. `claude-fable-5` for analyst and doctrine (`config.py:86,318`). `OPENAI_MODEL`/`GEMINI_MODEL` (`config.py:47-48`) are unused. Realised: 4-day outage on one billing account (F-36). "API_UNAVAILABLE" label does not exist (F-37)
- **Problem:** Analyst and doctrine both depend on `claude-fable-5`. Fable 5 access was suspended for all customers from 2026-06-12 until 2026-07-01 under a US export-control directive. NEXUS degraded safely (FLAT, `API_UNAVAILABLE`) but would go idle for the duration.
- **Recommendation:** A configurable fallback chain (e.g. Fable → Opus 5.5) with the issuing model recorded on every doctrine and signal, and statistics split by model. The fallback must never be silent.
- **Effort:** S · **Change risk:** Low.

**F-21 · MEDIUM · Ring 2 · Model IDs are a generation behind (not urgent)**
- **Status (verified 2026-09-29):** **CONFIRMED** (IDs). `config.py:45,46,86,318`. Retirement dates and prices not re-verified
- **Evidence:** `claude-fable-5` is active with retirement not before 2027-06-09; `claude-sonnet-4-6` not before 2027-02-17. Current: `claude-fable-5-1` ($10/$50), `claude-opus-5-5` ($4/$20), `claude-sonnet-5` ($2/$10).
- **Recommendation:** Upgrade deliberately, as its own task, with per-model logging (F-20). **Do not swap models mid-evaluation**; it changes the thing being measured. `temperature`/`top_p`/`top_k` are deprecated on current models and removed from the Python SDK ≥ 1.0 — NEXUS does not pass them (verified in the audited sources), so the 1.7.0 SDK on Frankfurt is safe.
- **Effort:** S · **Change risk:** Low.

**F-22 · MEDIUM · Ring 3 · Dead sensors in the live loop**
- **Status (verified 2026-09-29):** **CONFIRMED**. `comex_stocks` 0 rows, `basis_readings` 0 rows, `etf_holdings.gld_tonnes` NULL 14/14. Journal: 2,600× "empty frame for XAUUSD=X", 384× for GC=F, 76 COMEX error/warn lines
- **Problem:** COMEX stocks fail every cycle (likely bot protection on cmegroup.com — hypothesis), `basis` fails every cycle, GLD tonnage is always `None`. They produce log noise and NULL dimensions.
- **Recommendation:** Remove COMEX from the live loop (ingest weekly by hand or from a mirror if the ratio is wanted). GLD tonnage from SPDR's daily "total gold in trust" or the World Gold Council's weekly ETF data. Disable `basis` until the MT5 spot leg exists (F-13).
- **Effort:** S · **Change risk:** Low.

**F-23 · MEDIUM · Ops · Host hygiene**
- **Status (verified 2026-09-29):** **PARTIAL**. 2 GB host confirmed (hostname `ubuntu-s-1vcpu-2gb-fra1-01`); NEXUS peak 232 MB. sudo, SSH and pending reboot are not observable (hypothesis)
- **Problem:** `sudo` broken; root password SSH; kernel reboot pending; 2 GB RAM shared with other projects.
- **Recommendation:** Repair sudo (check the `Plugin` lines in `/etc/sudo.conf`; reinstall the `sudo` package); SSH keys only and `PasswordAuthentication no`; fail2ban; a scheduled reboot window (stop NEXUS, reboot, verify boot log and a dead-man ping); `MemoryMax=` in the unit so NEXUS and the other tenants cannot starve each other. Before MICRO, a dedicated droplet.
- **Effort:** S · **Change risk:** Low–medium (SSH changes can lock you out — keep a second session open).

**F-24 · MEDIUM · Ring 2 · RAG is write-only**
- **Status (verified 2026-09-29):** **CONFIRMED**. `rag.recall` (`fusion/rag.py:148`) has no caller outside `fusion/rag.py`. RAG is also numerically blank (F-35)
- **Recommendation:** Keep recall out of prompts until its value is measured: add a nightly recall hit-rate and a "precedent vs outcome" agreement metric; decide with data.
- **Effort:** S · **Change risk:** None.

**F-25 · MEDIUM · Ring 1 · The "scalp" pods run at H1**
- **Status (verified 2026-09-29):** **CONFIRMED**. Pods read the `1h` timeframe (`exec_/pod_agent.py:121-122`); S4 refused at H1 (`:105`)
- **Problem:** Pods evaluate 5-minute polls of H1 bars. The cost gate was designed for scalps. S2 is negative even at its replay upper bound.
- **Recommendation:** Freeze pod development until tick data exists. Evaluate S1/S2 honestly as H1 mean-reversion strategies with pre-registered kill criteria.
- **Effort:** — · **Change risk:** None.

**F-26 · MEDIUM · Ring 2 · Structured outputs are not used**
- **Status (verified 2026-09-29):** **CONFIRMED**, low value. Free-text `json.loads` parsing (`ai/doctrine.py:185,541`, `ai/analysis.py:307`). But Frankfurt had **0** true parse failures while credits lasted: the single PARSE_FALLBACK was a failed call
- **Recommendation:** Add Claude structured outputs (`output_config.format` with a JSON schema) as a **belt** to cut parse failures. The pydantic wall stays: constrained decoding guarantees format, not truth. Confirm the Fable model supports it before switching.
- **Effort:** S · **Change risk:** Low. 🔒 for the doctrine path (amendment).

**F-27 · MEDIUM (HIGH if OOM) · Ops · Unexplained restart ≈ 06:42 UTC 2026-09-23**
- **Status (verified 2026-09-29):** **REFUTED**. Clean SIGTERM stop, not OOM (journal 14949–14957). A second clean restart on 2026-09-25 06:49 (25112–25120) was unknown to the draft
- **Recommendation:** Run Q9 (journal) and the OOM check. If OOM: `MemoryMax`, a memory profile of the 16 agents, and F-23's dedicated droplet move sooner.

### LOW

| ID | Component | Problem | Recommendation | Status (2026-09-29) |
|---|---|---|---|---|
| F-28 | Ops | venv inside the checkout (`git clean` could delete the interpreter) | Move to `/home/nexus/venv`, update the unit | **CONFIRMED** + drift: 1,808 journal traceback paths show `/home/nexus/nexus/venv/…`, but the repo unit expects `/home/nexus/venv` (`ops/deploy/nexus.service:44`). The unit running on the server is not the repo's unit (F-41) |
| F-29 | Ring 1 | ATR helper duplicated (`pod_agent.AtrTracker` vs `replay._AtrTracker`) | Unify before any replay statistic supports a promotion | **CONFIRMED**: `exec_/pod_agent.py:55` `AtrTracker`, `tests/replay.py:143-150`, plus a third at `exec_/position_engine.py:417` |
| F-30 | Ring 1 | No partial-close primitive (SimBridge closes the whole position at TP1) | Required before F-02 and before SHADOW | **CONFIRMED**: `RealMT5Bridge` close sends the full `target.volume` (`data/mt5_bridge.py:419`). SimBridge realises P&L on the whole `position["lots"]` (`:208-211`) |
| F-31 | Link | Accept-time eviction lets anyone who can reach the port flap the brain | WireGuard or firewall pinning before the boxes split; `ufw` is SSH-only today | **CONFIRMED**: `link/channel.py:282-300`: an accepted socket replaces and closes the live connection before any frame is HMAC-authenticated |
| F-32 | Ring 2 | Stale log text "RULE5 … until Task 8" | Fix at next legitimate touch | **CONFIRMED**: `risk/validator.py:335` (🔒). The text appears in every WAIT line of the journal |
| F-33 | Tests | Nothing stops the suite from running against production | `conftest.py` database-name guard (part of F-05) | **CONFIRMED**: `tests/conftest.py` does not exist. Tests connect to `config.DATABASE_URL` (e.g. `tests/test_learning_loop.py:64`) and some run whole-table `DELETE`s inside rolled-back transactions (`:68,427-429,669`) |
| F-34 | Data | Signal ids jumped to 5314 | Cosmetic; corroborates F-05 | **CONFIRMED**: Ids 7–1366 (23 rows, iMac era), then 5314–5495 |

---

## 5. Safety audit

### 5.1 The sacred invariants

| # | Invariant | Status | Evidence |
|---|---|---|---|
| 1 | STAGE defaults to PAPER and changes only by human config edit + restart | **VERIFIED** | `config.py`: `_STAGE` read once at import, `lru_cache` getter, no setter; repo-wide grep test `test_stage_immutable`; `risk/stage.py` import-time decisions with an AST self-audit proving no function rebinds them (Task 13 audit); the preflight refuses any stage but PAPER |
| 2a | The AI never emits raw prices | **VERIFIED** (swing analyst) | `SignalAnchors` has no price fields and `extra="forbid"` (Task 2 audit, lines 68–106); 23-case fuzz; raw-price injection rejected |
| 2b | The AI never emits raw orders | **VERIFIED** | Doctrine cage (Task 15 audit): every failure → `FLAT_FALLBACK`; the analyst's output passes resolver → validator → sizer |
| 2c | Pydantic strictness is untouchable | **VERIFIED** | Strict, frozen models throughout; the doctrine's `model_validate_json` choice is documented and keeps strictness |
| 3 | The kernel imports nothing from the AI layer | **VERIFIED** | Import-table test via AST plus a clean-interpreter `sys.modules` sweep (Task 14) |
| 4 | Sizing, gates and breakers can be added to, never weakened | **VERIFIED** | Task 18 kernel amendment: `git diff \| grep "^-"` empty; three test lines adjusted for new PASS audit rows, disclosed |
| 5 | Every order path goes through `kernel.permit()` | **VIOLATED (swing path)** | Router: verified (Task 21 audit — reserve → permit → send, no bypass). Swing: the paper engine fills without the kernel (F-02). By design at PAPER when it was built; wrong by the invariant's intent |
| 6 | Every external call has a timeout, handling, a logged failure and a degradation path | **VERIFIED, with two gaps** | Every sensor fails closed (live-proven: COMEX, basis, BLS, credits exhaustion). Gaps: `RealMT5Bridge` timeouts are advisory only (synchronous C extension, flagged in Task 16); no external alert when the process itself dies (F-12) |
| 7 | Doctrine expires; AI silence means FLAT | **VERIFIED for pods · VIOLATED for swing** | `DoctrineHolder` expiry is correct (Task 15 audit). The analyst ignores the doctrine entirely (F-01) |

### 5.2 Specific questions from the mission prompt

- **Are stops broker-side?** UNTESTED — there is no broker. The `RealMT5Bridge` skeleton passes SL/TP in the order request, but it has never executed. Requirement for SHADOW: every order carries a broker-side stop; brain-side stop management is an addition, never the only protection.
- **What happens if the brain dies mid-trade?** At PAPER, nothing real. In the v7 topology the kernel and position engine run on the execution box beside the broker so they survive a brain outage; **today both run inside the brain process.** Requirement for SHADOW: the execution box runs kernel + position engine + broker-side stops locally, and the brain→exec link's silence degrades to doctrine expiry (already the designed behaviour, Task 17).
- **Is the fallback path tested?** Partly, and live: credit exhaustion in August produced `PARSE_FALLBACK` → FLAT across the doctrine path, with no crash. An export-control-style model suspension would take the same path. The kill switch was drilled once (iMac, 2026-08-07) and **has not been drilled on Frankfurt** — overdue under the promotion pack's own 30-day rule.

---

## 6. The next version of NEXUS (v8)

**Thesis: fewer paths, harder gates, honest numbers.** The evidence of the last three days says the binding constraint is governance and measurement, not information. v8 adds almost nothing new; it closes the gaps between what the architecture promises and what the code path does.

1. **One book, one path.** Every order — swing and pod, at every stage — goes `router → kernel → bridge`. At PAPER the bridge is SimBridge. The paper engine's independent fills retire. `positions` is the single book; `/status`, the promotion pack and the Observatory read it.
2. **Doctrine is law.** Every decision path reads `HOLDER.current()`. FLAT or expired stops new risk everywhere; `bias` filters direction; `risk_multiplier` scales size down. The cadence can never outlive the horizon.
3. **One thesis, one position.** Signals carry a `thesis_id`. A repeated idea refreshes the existing signal; it never multiplies exposure.
4. **Measurement you can trust.** Clean production data (test residue archived, test suite guarded). Promotion counts decisions, not rows. Probabilistic and deflated Sharpe instead of raw win rates. Pre-registered thresholds. A no-AI baseline. Forward-only evaluation of the LLM layer. A durable cost ledger at real prices.
5. **Broker truth at SHADOW.** MT5 bars, ticks, spread and fills become the price source. Futures demote to a sensor (the basis leg). The kernel and position engine move next to the broker, with broker-side stops.
6. **Operational maturity.** Nightly off-box backups, an external dead-man's switch, a lockfile and CI against a disposable database, repaired `sudo`, key-only SSH, memory limits.
7. **See it, then grade it.** The Observatory reads the audit trail that already exists (Appendix B); the Head Architect agent grades the desk from a deterministic metrics pack and can only advise (Appendix C).

**Stop building (for now):** more sensors (the v6 physical-flow layer, Shanghai premium, options walls); S4 before tick data; RAG in prompts before recall is measured; new pods before one pod shows an edge; any model upgrade mid-evaluation. Every one of those adds surface area to a system whose existing surface is not yet governed.

**Honest ceiling.** Nothing here makes NEXUS profitable. It makes it *measurable*. The current evidence neither shows nor rules out an edge, because the clone defect and the missing kernel on the swing path dominate every number. After Phases A and B below, 30–60 days of clean forward data will answer the only question that matters — does the AI desk beat its own no-AI baseline after costs — and the answer may be no. That is still a good outcome: it is cheap at PAPER.

---

## 7. Resources

Facts in this section were checked against live sources on 2026-09-23 (the architect's research pass); links are the canonical entry points. Items marked *not re-verified* rely on general knowledge — check them before depending on them.

### 7.1 Anthropic

| Resource | Link | Cost | Why it matters |
|---|---|---|---|
| Models overview (IDs, prices, context) | https://platform.claude.com/docs/en/models/overview | — | Current IDs: `claude-fable-5-1` $10/$50, `claude-opus-5-5` $4/$20, `claude-sonnet-5` $2/$10, `claude-haiku-4-5-20251001` $1/$5 per MTok (F-08, F-21) |
| Model deprecations | https://platform.claude.com/docs/en/about-claude/model-deprecations | — | `claude-fable-5` active, retirement not before 2027-06-09; `claude-sonnet-4-6` not before 2027-02-17; sampling parameters deprecated on current models and removed from the Python SDK ≥ 1.0 |
| Pricing (batch, caching) | https://platform.claude.com/docs/en/about-claude/pricing | Batch −50% | Nightly learning-loop and Architect jobs can run on the Batch API; batch and caching discounts stack |
| Structured outputs | https://platform.claude.com/docs/en/build-with-claude/structured-outputs | — | Constrained JSON as a belt behind the pydantic wall (F-26); confirm Fable support first |
| Fable 5 access statement | https://www.anthropic.com/news/fable-mythos-access | — | 2026-06-12 → 2026-07-01 suspension under a US export-control directive; basis for F-20 |

### 7.2 Broker, market and data

| Resource | Link | Cost | Why |
|---|---|---|---|
| MetaTrader5 Python package | https://pypi.org/project/MetaTrader5/ (v5.0.5735 per depscope) | Free | Windows-only; the reason a Windows execution box exists |
| pdmt5 (pandas wrapper, dry-run mode) | https://pypi.org/project/pdmt5/ | Free | Typed MT5 helpers; states the Windows requirement explicitly |
| EBC — XAUUSD position mechanics | https://www.ebc.com/forex/how-to-trade-gold-online | — | 100 oz per lot; commission on Professional accounts only; swap at rollover |
| EBC — trading-condition notices | https://www.ebc.com/forex/best-day-trading-platform | — | Leverage reduced before the daily close and around releases (from 2026-07-03); late-Friday restrictions (F-16) |
| EBC account types (third-party review — verify on EBC's own page) | https://tradersunion.com/brokers/forex/view/ebc-financial-group/ | — | Standard: no commission, cost in the spread. Professional: $5,000 minimum, per-lot commission. Decides `COMMISSION_USD_PER_LOT` |
| Rollover / session behaviour | https://www.startrader.com/knowledge-intermediate/xauusd-trading-hours-open-close-best-times-to-trade-gold/ | — | Spread widening and slippage around ~5 pm New York |
| ForexFactory export limits | https://www.forexfactory.com/thread/post/15159093 | Free | Max 2 downloads per 5 minutes per IP; file updated hourly; an HTML "Request Denied" page on excess (NEXUS polls every 4 h — fine; watch shared-IP neighbours) |
| CFTC COT historical files | https://www.cftc.gov/es/node/132396 | Free | Yearly disaggregated files since 2010 for percentile backfills |
| CME Registrar reports (Gold Stocks) | https://www.cmegroup.com/clearing/operations-and-deliveries/registrar-reports.html | Free | Source of COMEX registered/eligible stocks (F-22) |
| World Gold Council — ETF holdings & flows | https://www.gold.org/goldhub/data/gold-etfs-holdings-and-flows | Free | Weekly updates (Mondays), monthly xlsx — the reliable ETF-tonnage source |
| SPDR Gold Shares | https://www.spdrgoldshares.com/usa/ | Free | "Total gold in trust" in tonnes, updated each weekday |
| FRED API | https://fred.stlouisfed.org/docs/api/fred/ | Free | 120 requests/minute per key (NEXUS uses ~6/hour) |
| GDELT DOC 2.0 | https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/ | Free, no key | 1 request / 5 s; `TimelineVol` and `TimelineTone` modes — the basis for `news_heat_z` (F-10) |

### 7.3 Engineering and operations

| Resource | Link | Cost | Why |
|---|---|---|---|
| Healthchecks.io | https://healthchecks.io | Free (20 checks); self-hostable (BSD-3) | External dead-man's switch with Telegram alerts (F-12) |
| NautilusTrader | https://nautilustrader.io | Free | Event-driven engine with identical strategy code in backtest and live |
| vectorbt | https://vectorbt.dev | Free / Pro | Fast vectorized parameter screening |
| backtesting.py | https://kernc.github.io/backtesting.py/ | Free | Simple, fast to learn; backtrader development has stalled |
| TradingView Lightweight Charts v5 | https://www.tradingview.com/lightweight-charts/ | Free (attribution) | Observatory price chart; v5 moved series markers into a separate plugin, ES2020 only, multi-pane |
| Tailscale *(not re-verified in this pass)* | https://tailscale.com | Free personal tier | Private network for the Observatory — never expose it publicly |
| QuantStats *(not re-verified)* | https://github.com/ranaroussi/quantstats | Free | Tearsheets for the Architect's metrics pack |

### 7.4 Statistics and evidence on LLM trading

| Resource | Link | Why |
|---|---|---|
| Bailey & López de Prado, *The Deflated Sharpe Ratio* (2014) | https://papers.ssrn.com/abstract=2460551 | Corrects for selection bias across many trials and for non-normal returns — the right promotion statistic once several configs have been tried |
| Bailey, Borwein, López de Prado & Zhu, *The Probability of Backtest Overfitting* | https://papers.ssrn.com/abstract=2326253 | CSCV method for estimating overfitting probability in the replay harness |
| Bailey & López de Prado, *The Sharpe Ratio Efficient Frontier* (PSR, minimum track-record length) | https://www.risk.net/journal-risk/2223785/sharpe-ratio-efficient-frontier | How long a track record must be before a Sharpe claim is credible |
| AI-Trader live benchmark (2026) | https://arxiv.org/html/2512.10971v1 | Most LLM agents showed poor returns and weak risk management live |
| StockBench | https://stockbench.github.io/ | Most LLM agents struggle to beat buy-and-hold |
| Memory-controlled trading benchmark (2026) | https://arxiv.org/html/2605.28359v1 | Returns largely explained by passive market and style exposure; leakage is real (F-18) |
| Agent Market Arena | https://arxiv.org/abs/2510.11695 | Agent architecture matters more than the model backbone — supports investing in governance, not model swaps |

---

## 8. Prioritized roadmap

Same law as IMPLEMENTATION-V7: one task per session, branch `task/<id>-*` created as the **first** action, acceptance test green, report A–F (with `git log --oneline -3 main`), architect verdict before merge. Tasks touching 🔒 files carry an amendment notice and the additive-only grep proof.

### Phase A — Stop the bleeding and make the numbers honest (PAPER)

| Task | Objective | Files | Acceptance (summary) |
|---|---|---|---|
| **A1** Doctrine binds the analyst | F-01, F-19 (swing half) | `ai/analysis.py`, `config.py`, `tests/test_analysis.py` | FLAT / expired / `swing=False` → `DOCTRINE_BLOCKED` with **zero** Claude calls (mock asserts); `LONG_ONLY` rejects a parsed SHORT; `risk_multiplier=0.25` → sizer receives 0.25%; a `DOCTRINE_GATE` row in `validator_log`; live: a FLAT period produces no new signals |
| **A2** One thesis, one position | F-03, F-04 | `ai/analysis.py`, `exec_/paper_engine.py`, migration `017_thesis.sql` (`signals.thesis_id`), `config.py`, tests | Five identical cycles → one signal plus four `REAFFIRMED` events; TTL expiry at the configured hours; a drifted fill is refused with a logged reason |
| **A3** Cadence ≤ horizon | F-06 | 🔒 `ai/doctrine.py` (`run_doctrine_agent` only), tests | Simulated quiet session with 15 m horizons → zero expiries over 24 simulated hours; grep proof shows no modified lines outside the function |
| **A4** Cost ledger at real prices | F-08 | migration `018_api_usage.sql`, `ai/analysis.py`, 🔒 `ai/doctrine.py` (`_call_model` accounting only), `fusion/learning_loop.py`, `config.py` | One row per call with model and tokens; the cap trips from the DB sum; a restart does not reset it; hand-computed cost for a known usage matches list price |
| **A5** Production data hygiene | F-05, F-33 | `ops/hygiene/archive_test_rows.py` (dry-run default), `tests/conftest.py` | Dry run lists rows by provenance; the real run moves them to `*_archive` after a verified `pg_dump`; the suite refuses a non-`nexus_dev`/`*_test` database; the promotion pack re-run on clean data |
| **A6** Backups + dead-man's switch | F-09, F-12 | `ops/deploy/backup.sh`, `ops/deploy/nexus-backup.service` + `.timer`, `backend.py` (heartbeat ping) | A restore of last night's dump into a scratch DB reproduces the row counts; stopping NEXUS for 4 minutes produces a Telegram alert from healthchecks.io |
| **A7** Kill drill on Frankfurt | Invariant 7 hygiene | none (operator drill) | `/killswitch` → CONFIRM → journal shows the halt → `/clearkill` → restart; `command_audit` rows pasted |

### Phase B — One book (still PAPER)

| Task | Objective | Files | Acceptance (summary) |
|---|---|---|---|
| **B1** Swing through the router and kernel | F-02, F-15 | `exec_/paper_engine.py` (adapter or retirement), `exec_/position_engine.py`, `exec_/router.py`, `data/mt5_bridge.py` (SimBridge partial close, F-30), `ops/telegram_bot.py`, tests | A swing signal produces a `positions` row via reserve-before-send; the 3rd concurrent position is denied `MAX_POSITIONS`; a scripted −2R day halts on `DAILY_LOSS`; `/status` shows one book in R and $ |
| **B2** Rollover / Friday / weekend | F-16 | 🔒 `risk/kernel.py` (additive breakers), `config.py`, tests | Orders inside the window denied with the new breaker names; existing kernel tests unmodified; grep proof |
| **B3** Honest sensors | F-10, F-11, F-22 | `sensors/news.py`, `fusion/regime.py`, `sensors/positioning.py`, `config.py`, tests | `news_heat_z` distribution centred near 0 on 30 days of history; HMM trains on daily FRED history; RULE 5 fires in a replayed `RISK_OFF` scenario; COMEX removed from the loop |
| **B4** Baselines and decision-level statistics | F-07, F-17, F-18 | `ops/baselines.py`, `ops/promotion.py`, `config.py`, tests | Three baselines booked through SimBridge daily; promotion pack reports effective N, per-decision expectancy, PSR, and analyst-minus-baseline; LLM strategies scored forward-only |
| **B5** Host hardening | F-14, F-23, F-27, F-28 | lockfile, CI workflow, unit file, runbook addendum | CI green on a disposable Postgres; production installs from the lock; key-only SSH; `sudo` works; `MemoryMax` set; restart cause documented |

### Phase C — The Observatory (the mission prompt's Phase 2, redesigned — Appendix B)

C1 read-only roles and the event projector · C2 FastAPI read-only API and WebSocket (Tailscale-bound) · C3 Next.js frontend, eight screens.

### Phase D — The Head Architect agent (the mission prompt's Phase 3 — Appendix C)

D1 deterministic metrics pack · D2 Architect call with a strict schema and evidence validation · D3 findings workflow and scheduling · D4 the first `HEAD_ARCHITECT_CONCLUSION.md` over all history.

### Phase E — SHADOW readiness (Windows execution box, demo account only)

E1 Windows VPS + EBC **demo** login · E2 `RealMT5Bridge` verified call by call, including partial close and broker-side SL/TP · E3 MT5 as the price, spread, slippage and basis source (F-13) · E4 kernel + position engine on the execution box · E5 link wired over WireGuard (F-31) · E6 kill drill on the real topology.

### Pre-registered promotion numbers (proposal — replaces the current `PROMOTION_CRITERIA` after Phase B)

Counts are **decisions** (distinct `thesis_id`), measured only on data produced after Phase B merges. Any strategy-config change restarts that strategy's clock.

| Transition | Criteria (all required) |
|---|---|
| PAPER → SHADOW (a mechanics gate, not an edge gate) | ≥ 30 days on the one-book path · uptime ≥ 97% by dead-man pings · zero invariant violations · zero clone clusters · doctrine fallback ≤ 15% of horizon-hours · kill drill ≤ 30 days old · a verified restore from backup |
| SHADOW → MICRO | ≥ 30 days on real spreads · ≥ 100 decisions · expectancy per decision > 0 with **PSR ≥ 0.95** against 0 · analyst expectancy minus the deterministic baseline > 0 · realized slippage ≤ the model's p75 · zero reconciliation incidents |
| MICRO → SCALED | ≥ 45 days live micro · ≥ 150 decisions · net positive after all real costs · **Deflated Sharpe ≥ 0.95** using the honest count of configurations ever tried · max drawdown ≤ 6R · 48-hour cooling-off, then the operator's edit |

Why 100 is a floor, not a comfort: with 100 independent trades and an observed 50% hit rate, the 95% Wilson interval is roughly 40%–60%. For a 1.5R-target strategy the breakeven hit rate is 40%, so the lower bound only just clears it. Fewer decisions than that cannot distinguish a real edge from luck.

---

## 9. Open questions for the operator

1. **V-0:** confirm no broker credential (live or demo) exists on any machine.
2. Confirm solo operation — the mission prompt assumes two people.
3. The monthly API budget, and a separate cap for the Architect agent.
4. Where should the Observatory run: on the shared 2 GB Frankfurt box (API only, static frontend) or on another host reaching Frankfurt over Tailscale?
5. Does Windows decision "B" (defer) stand, or does Phase E start after Phase B?
6. Which EBC account type is intended for MICRO — Standard (cost in the spread) or Professional (commission)? It changes the cost-gate constants.
7. Archive (recommended) or delete the test residue in production?
8. Keep S1/S2 running at H1 in the meantime, or freeze pods until tick data?

---

## Appendix A — Verification queries (read-only)

Open a session as the service user and paste queries one at a time. `count(1)` is used instead of `count(*)` on purpose: it is equivalent, and it survives chat clients that eat asterisks.

```
su - nexus -c 'psql -d nexus'
```

**Q1 — swing outcomes, last 7 days**
```sql
SELECT status, count(1) AS n, round(sum(outcome_r)::numeric, 2) AS total_r
FROM signals WHERE outcome_ts > now() - interval '7 days'
GROUP BY status ORDER BY n DESC;
```

**Q2 — clone clusters (fills per minute)**
```sql
SELECT date_trunc('minute', filled_at) AS fill_minute, direction, count(1) AS n,
       min((prices->>'entry')::numeric) AS min_entry, max((prices->>'entry')::numeric) AS max_entry
FROM signals WHERE filled_at > now() - interval '7 days'
GROUP BY 1, 2 ORDER BY 1;
```

**Q3 — validator decisions, last 7 days**
```sql
SELECT rule_name, rule_result, count(1) FROM validator_log
WHERE ts > now() - interval '7 days' GROUP BY 1, 2 ORDER BY 1, 2;
```

**Q4 — kernel decisions, last 7 days**
```sql
SELECT breaker, action, count(1) FROM kernel_events
WHERE ts > now() - interval '7 days' GROUP BY 1, 2 ORDER BY 3 DESC;
```

**Q5 — doctrine sources and horizons, last 7 days**
```sql
SELECT source, bias, count(1), round(avg(review_horizon_min)) AS avg_horizon_min
FROM doctrines WHERE ts > now() - interval '7 days' GROUP BY 1, 2 ORDER BY 3 DESC;
```

**Q6 — stage_events provenance (test residue shows as bursts)**
```sql
SELECT date_trunc('day', ts) AS day, event, env_stage, effective_stage, count(1)
FROM stage_events GROUP BY 1, 2, 3, 4 ORDER BY 1;
```

**Q7 — why the regime HMM will not train**
```sql
SELECT count(1) AS total, count(real_yield_5d_delta) AS ry5d, count(dxy) AS dxy,
       count(curve_2s10s) AS curve, count(news_heat) AS heat
FROM state_vectors;

SELECT count(1) AS gaps_over_1h
FROM (SELECT ts - lag(ts) OVER (ORDER BY ts) AS gap FROM state_vectors) g
WHERE gap > interval '1 hour';
```

**Q8 — signal age at fill**
```sql
SELECT id, ts AS created, filled_at, filled_at - ts AS age
FROM signals WHERE filled_at IS NOT NULL ORDER BY age DESC LIMIT 20;
```

**Q9 — the 2026-09-23 restart (shell, as root; the server clock is UTC)**
```
journalctl -u nexus --since "2026-09-23 06:00" --until "2026-09-23 07:30" --no-pager | grep -E "Main process|Started|Stopped|Traceback|CRITICAL|Killed" | head -50
dmesg -T | grep -i -E "out of memory|oom|killed process" | tail -20
```

**Q10 — test-residue sniff**
```sql
SELECT symbol, count(1) FROM signals GROUP BY symbol ORDER BY 2 DESC;
SELECT symbol, count(1) FROM validator_log GROUP BY symbol ORDER BY 2 DESC;
SELECT source, count(1), min(ts), max(ts) FROM doctrines GROUP BY source;
```

**Q11 — last kill drills**
```sql
SELECT ts, command, outcome FROM command_audit
WHERE command IN ('/killswitch', '/clearkill') ORDER BY ts DESC LIMIT 6;
```

**Q12 — the router book**
```sql
SELECT source, state, count(1) FROM positions GROUP BY 1, 2 ORDER BY 1, 2;
```

---

## Appendix B — The Observatory, redesigned (mission Phase 2)

### B.1 Principle: read the audit trail; do not instrument locked files

The mission prompt asks for emitters in every ring. In this codebase most emitters would land in 🔒 files (kernel, validator, doctrine, resolver), and every one is a change to the trading process. They are also unnecessary: NEXUS already writes a structured audit record for nearly every decision.

| Existing table | Event it already records | Ring |
|---|---|---|
| `validator_log` | Every rule decision per analysis cycle, with the numbers | 2 |
| `doctrines` | Every posture, its raw model response and its source (FABLE / PARSE_FALLBACK / EXPIRY_FALLBACK) | 2 |
| `signals` | Persisted → PENDING → OPEN → STOPPED/BE/TP2/EXPIRED, with R, MAE, MFE | 2 → 1 |
| `positions`, `fills` | Reserve, send, fill, slippage, spread at send, close | 1 |
| `kernel_events` | Every ALLOW / DENY / CLAMP / PASS / FLATTEN / HALT / DEMOTE, with full context | 0 |
| `stage_events`, `command_audit` | Governance and every operator command | 0 / ops |
| `state_vectors`, `macro_observations`, `cot_reports`, `econ_events`, `news_articles` | Sensor output (freshness = latest `ts` per table) | 3 |

The only genuinely missing data is process health and per-sensor freshness, which live in memory. **One** new writer closes that gap: `backend.py` (not locked) writes an `ops_heartbeats` row every 30 s — alive / registered / dead counts, RSS memory, stage, doctrine age, budget used, last `market_update` time and a per-sensor freshness map.

### B.2 The projector

- A separate systemd unit (`nexus-observatory`), its own OS user, the read-only `observatory` DB role (Appendix D).
- Polls each audit table by id watermark every 1–2 s with a bounded `LIMIT`, normalizes rows into one event shape: `{id, ts, ring, agent, event_type, severity, correlation_id, trade_id, payload}`, keeps a ring buffer, and pushes over WebSocket.
- If the prompt's `agent_events` table is wanted, the **projector** writes it — in a separate database, `nexus_ops`, never the trading database. "Fire-and-forget, never slows the trading loop" then holds by construction: the trading process publishes nothing at all.
- Correlation without new instrumentation: an analysis cycle's `validator_log` rows and its `signals` row share the same `ts` (the cycle's `utc_now`, per the Task 3/5 code); orders correlate by `client_order_id` across `positions`, `fills` and `kernel_events.context`; the doctrine in force at time *t* is the latest doctrine with `ts ≤ t < ts + horizon`.
- Tests: stop the Observatory and assert the backend heartbeat cadence is unchanged; every projector query completes under the role's `statement_timeout`. Indexes on `ts` for the busy tables arrive through an approved migration using `CREATE INDEX CONCURRENTLY`.

### B.3 The API

FastAPI, read-only. `GET /api/v1/state` (stage, equity — labelled "SimBridge-derived" at PAPER — today's R, open positions, breaker states, KILL-file state, doctrine and expiry countdown), `/events?since_id=&ring=&severity=`, `/trace/signal/{id}`, `/trace/order/{client_order_id}`, `/perf?window=`, `/health`, `/architect/reports`; `WS /ws/events`. **No endpoint can write to the trading database.** Bearer-token auth, bound to the Tailscale interface only, with the port closed in `ufw`.

### B.4 The frontend

Next.js (App Router) + TypeScript + Tailwind, built as a **static export** on the iMac or in CI and served by FastAPI — no Node server on the shared 2 GB box. TradingView Lightweight Charts v5 (series markers are a separate plugin in v5). The eight screens from the mission prompt: Command, Ring map, Event feed, Decision trace, Price chart (entries, exits, stops and validator-rejected cycles at their resolved prices), Performance (decision-level, from Phase B4), System health (heartbeats, sensor freshness, API spend from the ledger against the cap, restarts), and Architect. WebSocket auto-reconnect with a visible STALE banner when the last event is older than twice the heartbeat. Every panel has an explicit "no data yet" state; nothing placeholder is ever rendered as real. Phone-first layout.

### B.5 Resource budget

Projector + API target < 150 MB RSS, enforced with `MemoryMax=` on the unit. Runs as its own OS user with its own `EnvironmentFile` holding only its DB credentials.

---

## Appendix C — The Head Architect agent (mission Phase 3)

- **C.1 Metrics pack (deterministic, no AI).** Python + SQL, JSON output for day / week / all-time, every metric paired with its sample size. Key naming such as `perf.swing.week.decisions`, `perf.swing.week.expectancy_r`, `perf.swing.week.psr`, `gates.RULE0_ENTRY_DRIFT.WAIT.week`, `kernel.DAILY_LOSS.HALT.all`, `doctrine.fallback_hours_pct.week`, `doctrine.expiries.week`, `cost.usd.week`, `cost.usd_per_decision.week`, `ops.uptime_pct.week`, `ops.restarts.week`, `data.freshness_s.fred`, `baseline.rule.expectancy_r.week`, `invariant.5.status`.
- **C.2 The call.** `claude-opus-5-5` (the current Opus ID, verified) with structured outputs if supported, validated by a strict pydantic `ArchitectReport` exactly as the mission prompt specifies. **Evidence validation:** every evidence item must be a key present in the pack or a `table:id` reference that exists (checked by SQL). Invalid findings are returned once with the error list; still invalid → dropped, logged, and counted in the report. Its own budget cap (default $1/day) recorded in the `api_usage` ledger with `purpose='architect'`; the nightly run uses the Batch API.
- **C.3 Advisory only.** Findings live in `nexus_ops.architect_findings` (`status` PROPOSED / APPROVED / DISMISSED / DONE, `decided_at`, `note`). Approving a finding in the Observatory writes only to `nexus_ops` and produces a **task-brief request** — never a change.
- **C.4 Schedule.** Nightly at 03:00 UTC after the learning loop. "Run review now" enqueues a row in `nexus_ops.architect_requests`; the agent polls. Reports are stored in the DB and exported to `docs/architect/reports/YYYY-MM-DD.md` from the dev checkout, so the production checkout stays clean.
- **C.5 Isolation.** Separate OS user, systemd unit, read-only DB role and `EnvironmentFile` (its API key and DSN only — no access to the trading service's `.env`).

**Final deliverable** (`docs/architect/HEAD_ARCHITECT_CONCLUSION.md`, after Phases A–D): the verdict on health, reason for any profit, and safety; CRITICAL/HIGH changes in order; what to improve next and what to stop building; whether PAPER is still right; the pre-registered promotion numbers; the top three tasks for the following week.

---

## Appendix D — Read-only roles (run as the Postgres superuser: `su - postgres -c psql`)

```sql
CREATE ROLE nexus_readonly NOLOGIN;
GRANT CONNECT ON DATABASE nexus TO nexus_readonly;
GRANT USAGE ON SCHEMA public TO nexus_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO nexus_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE nexus IN SCHEMA public GRANT SELECT ON TABLES TO nexus_readonly;

CREATE ROLE observatory LOGIN IN ROLE nexus_readonly;        -- then: \password observatory
ALTER ROLE observatory SET default_transaction_read_only = on;
ALTER ROLE observatory SET statement_timeout = '5s';
ALTER ROLE observatory CONNECTION LIMIT 5;

CREATE ROLE architect_agent LOGIN IN ROLE nexus_readonly;    -- then: \password architect_agent
ALTER ROLE architect_agent SET default_transaction_read_only = on;
ALTER ROLE architect_agent SET statement_timeout = '30s';
ALTER ROLE architect_agent CONNECTION LIMIT 3;
```

The real protection is the privilege set (SELECT only); `default_transaction_read_only` is a second belt that a session could override. **Acceptance:** connected as `observatory`, an `INSERT INTO kernel_events …` must fail with a permission error. Passwords are set interactively and never written to the repo or to chat.


---

## 10. Verification method (Phase 1 VERIFY, 2026-09-29)

- **Code:** `main` @ `c08ed90` ("task f1: frankfurt deploy kit"). Nothing was edited. The only file added is this document.
- **Database:** `nexus_prodcopy` only, via `psql`/`psycopg2` with `PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=60s -c timezone=UTC'`. A deliberate `CREATE TABLE` probe failed with "cannot execute CREATE TABLE in a read-only transaction". pytest was **not** run against it, or at all.
- **Copy extent:** the brief described the copy as of ≈ 2026-09-23. It is actually from **2026-09-29**: `doctrines` to 10:23:34 UTC, `state_vectors`/`candles` to 10:00 UTC, `signals` to 2026-09-25 11:53:08. The dump file in `~/nexus_evidence` is dated Sep 29 12:31. The extra six days are used as evidence throughout and are called out where they change a number.
- **Journal:** 2026-09-20 09:38:00 → 2026-09-29 10:32:58 UTC. `dmesg` is unavailable, so Q9 uses journal `Main process exited` / `Killed` lines (there are none) plus the stop sequence.
- **Appendix A queries** were run exactly as written. Where a query's output is misleading (Q3 counts 2099 test rows; Q2 `filled_at` is a bar timestamp), both the as-written result and a corrected read are given.
- **Counting rule:** decisions, not rows (§3.2).

### 10.1 Appendix A results

| Q | Result (as written) | Note |
|---|---|---|
| Q1 | 7-day window: STOPPED 71 (−71.00) · EXPIRED 43 · TP2 11 (+31.30) · BE 7 (+5.25). All time: STOPPED 132 · EXPIRED 45 · TP2 15 · BE 13 | all-time includes 23 iMac-era rows |
| Q2 | 21 fill bars, 139 fills; `filled_at` minute/second is always :00:00 | `filled_at` is an H1 bar open (F-39) |
| Q3 | every rule inflated by +86 rows dated 2099-06-01 08:00 | F-38. Real Frankfurt counts in §3.4 |
| Q4 | 0 rows | no production `permit()` ever |
| Q5 | PARSE_FALLBACK FLAT 258 · FABLE FLAT 171 · EXPIRY_FALLBACK FLAT 74 · FABLE BOTH 25 · avg horizon 15 min | 258 PARSE_FALLBACK = billing failures (F-37) |
| Q6 | 2026-08-02/06/07: 1,962 rows, incl. MICRO/SHADOW/SCALED boots and DEMOTION_WRITTEN; Frankfurt: 3 BOOT PAPER (ids 2099–2101) | §13 |
| Q7 | total 340 · `real_yield_5d_delta` 338 · `dxy` 339 · `curve_2s10s` 338 · `news_heat` 339 · gaps > 1 h: 4 | refutes the draft's NULL/gap hypothesis for F-11 |
| Q8 | top ages (to bar ts): #5340 18:42, #5341 18:12, #5314 17:21, #5315 16:51, #5371 16:30, #5459 16:28 | |
| Q9 | journal only: clean SIGTERM stops 2026-09-23 06:42:02 and 2026-09-25 06:49:45; no `Killed` / `Main process exited` | F-27 refuted |
| Q10 | `signals` 205/205 and `validator_log` 4,544/4,544 have symbol `GC=F`, so a symbol sniff finds no residue there. Doctrines: PARSE_FALLBACK 673 · EXPIRY_FALLBACK 117 · FABLE 503 | residue is identified by date instead (§13) |
| Q11 | one kill drill: 2026-08-07 (iMac) `/killswitch` CONFIRMED 11:19:38, `/clearkill` CONFIRMED 11:23:41. None on Frankfurt; Frankfurt `command_audit` ids 87–101 are `/status`, `/pods`, `/doctrine` and typos, last 2026-09-24 17:22 | drill overdue |
| Q12 | 0 rows | `positions` empty |

---

## 11. Verification table — F-01 … F-34

| ID | Status | One-line evidence |
|---|---|---|
| F-01 | **CONFIRMED** | No doctrine read in `ai/analysis.py`. `HOLDER.current` is consumed only at `exec_/pod_agent.py:217` and `exec_/position_engine.py:435`. 0 Frankfurt doctrines allowed swing, yet 182 signals were persisted. (The draft's "~90 in 36 h" is actually 58 fills.) |
| F-02 | **CONFIRMED** | `exec_/paper_engine.py` has no permit/kernel/router reference. Q4 = 0. Peak 27 open against `max_concurrent=2` (journal line 5). Worst days −29.75R and −38.40R |
| F-03 | **CONFIRMED** | Unconditional `INSERT … 'PENDING'` at `ai/analysis.py:421-440`. Every crossed PENDING fills on the same bar (`exec_/paper_engine.py:139-154`). 25 L at bar 09-21 03:00 (−25R); 19 S at 09-25 09:00 (−19R) |
| F-04 | **CONFIRMED** | `config.py:107` TTL 24 h; `exec_/paper_engine.py:133-137`. #5314 filled after 17.6 h (journal 4001). Median age at fill 5.3 h; 27 fills older than 12 h |
| F-05 | **CONFIRMED** | 1,962/1,965 `stage_events`, 4,840/4,840 `kernel_events`, 688 `validator_log` rows dated 2099, test doctrines (§13) |
| F-06 | **CONFIRMED** | Cadence 15/60 min (`config.py:322-323`) with sleep independent of the 15–120 min horizon (`ai/doctrine.py:101,704-734`). A live FABLE doctrine covered 67.7% of minutes; 57 EXPIRY_FALLBACK |
| F-07 | **CONFIRMED** | 139 rows = 21 fill-bar decisions = 7 entry-zone decisions (§3.2) |
| F-08 | **PARTIAL** | Constants 3.0/15.0 (`config.py:103-104`) reproduce the journal's `est_cost` exactly (line 64). The in-process counter resets on restart (`ai/doctrine.py:396-418`; `budget_spent=$0.0292` at 06:42:27 after the restart). The ×3.3 needs the real list price (not in repo or DB): hypothesis |
| F-09 | **PARTIAL** | The repo has only the one-off migration dump (`ops/deploy/RUNBOOK-FRANKFURT.md:149`) and no scheduled backup. Server cron and droplet backups are not observable: hypothesis |
| F-10 | **CONFIRMED** | `news_heat` = 1.00 in 317/340 `state_vectors`, 0.94–0.99 in 20 more |
| F-11 | **PARTIAL** | Never trained: CONFIRMED (journal 69, 15041, 25184; nightly `retrain_regime: not_trained rows 0`). The draft's cause (NULLs/gaps) is **REFUTED** by Q7. The real cause is `Decimal` rejection (F-35) |
| F-12 | **CONFIRMED** | Only an in-process heartbeat (`backend.py:220`). It logged `alive=10 … dead=0` on 2026-09-28 12:00:26 while every model call was failing (F-36) |
| F-13 | **CONFIRMED** | `config.py:55` `YF_SYMBOL="GC=F"`; spot `XAUUSD=X` (`config.py:392`) returns no data (2,600 journal errors) |
| F-14 | **PARTIAL** | `requirements.txt`: 15 names, zero pins (CONFIRMED). Installed Frankfurt versions are not visible in journal or DB (hypothesis) |
| F-15 | **CONFIRMED** | `/status` reads only `positions` (`ops/telegram_bot.py:244,260,344`). `positions` = 0 rows while `signals` has 139 fills |
| F-16 | **CONFIRMED** | End-of-week flat is pods-only (`exec_/position_engine.py:31,253`). There is no rollover or Friday logic in `risk/` |
| F-17 | **CONFIRMED** | No baseline strategy anywhere. "baseline" appears only as equity baselines in `risk/kernel.py:182,344` |
| F-18 | **CONFIRMED** (policy gap) | `tests/replay.py` replays pods only. No rule forbids replaying the LLM, and none is codified in the promotion pack |
| F-19 | **CONFIRMED** | `risk_multiplier` and `swing_signals_allowed` are read only by `ai/doctrine.py` and displayed at `ops/telegram_bot.py:379,381`. Pods use fixed `S1–S4_LOTS=0.01` (`config.py:377,384,404,414`) |
| F-20 | **CONFIRMED** | `claude-fable-5` for analyst and doctrine (`config.py:86,318`). `OPENAI_MODEL`/`GEMINI_MODEL` (`config.py:47-48`) are unused. Realised: 4-day outage on one billing account (F-36). "API_UNAVAILABLE" label does not exist (F-37) |
| F-21 | **CONFIRMED** (IDs) | `config.py:45,46,86,318`. Retirement dates and prices not re-verified |
| F-22 | **CONFIRMED** | `comex_stocks` 0 rows, `basis_readings` 0 rows, `etf_holdings.gld_tonnes` NULL 14/14. Journal: 2,600× "empty frame for XAUUSD=X", 384× for GC=F, 76 COMEX error/warn lines |
| F-23 | **PARTIAL** | 2 GB host confirmed (hostname `ubuntu-s-1vcpu-2gb-fra1-01`); NEXUS peak 232 MB. sudo, SSH and pending reboot are not observable (hypothesis) |
| F-24 | **CONFIRMED** | `rag.recall` (`fusion/rag.py:148`) has no caller outside `fusion/rag.py`. RAG is also numerically blank (F-35) |
| F-25 | **CONFIRMED** | Pods read the `1h` timeframe (`exec_/pod_agent.py:121-122`); S4 refused at H1 (`:105`) |
| F-26 | **CONFIRMED**, low value | Free-text `json.loads` parsing (`ai/doctrine.py:185,541`, `ai/analysis.py:307`). But Frankfurt had **0** true parse failures while credits lasted: the single PARSE_FALLBACK was a failed call |
| F-27 | **REFUTED** | Clean SIGTERM stop, not OOM (journal 14949–14957). A second clean restart on 2026-09-25 06:49 (25112–25120) was unknown to the draft |
| F-28 | **CONFIRMED** + drift | 1,808 journal traceback paths show `/home/nexus/nexus/venv/…`, but the repo unit expects `/home/nexus/venv` (`ops/deploy/nexus.service:44`). The unit running on the server is not the repo's unit (F-41) |
| F-29 | **CONFIRMED** | `exec_/pod_agent.py:55` `AtrTracker`, `tests/replay.py:143-150`, plus a third at `exec_/position_engine.py:417` |
| F-30 | **CONFIRMED** | `RealMT5Bridge` close sends the full `target.volume` (`data/mt5_bridge.py:419`). SimBridge realises P&L on the whole `position["lots"]` (`:208-211`) |
| F-31 | **CONFIRMED** | `link/channel.py:282-300`: an accepted socket replaces and closes the live connection before any frame is HMAC-authenticated |
| F-32 | **CONFIRMED** | `risk/validator.py:335` (🔒). The text appears in every WAIT line of the journal |
| F-33 | **CONFIRMED** | `tests/conftest.py` does not exist. Tests connect to `config.DATABASE_URL` (e.g. `tests/test_learning_loop.py:64`) and some run whole-table `DELETE`s inside rolled-back transactions (`:68,427-429,669`) |
| F-34 | **CONFIRMED** | Ids 7–1366 (23 rows, iMac era), then 5314–5495 |

---

## 12. V-0 — broker credential, fill mode, stage

**Answer: no trace of a broker credential, of a fill mode other than MODELED, or of a non-PAPER stage in production.** Limits: this session cannot see the Frankfurt `.env` or any Windows machine. The answer covers the repo, git history, the production DB copy and the production journal.

| Check | Result | Evidence |
|---|---|---|
| Credential **names** read by code | `ANTHROPIC_API_KEY, DATABASE_URL, FINNHUB_API_KEY, FRED_API_KEY, GEMINI_API_KEY, HMAC_SECRET, NEXUS_STAGE, OPENAI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS`. None is a broker login | grep of `os.environ`/`os.getenv` across `*.py` (values never printed) |
| MT5 login path | `RealMT5Bridge` calls `mt5.initialize()` with no arguments (`data/mt5_bridge.py:298`). A credential could only live in a Windows MT5 terminal, and none exists | code |
| Bridge in production | `BRIDGE_KIND = "SIM"` (`config.py:337`). Journal: 3× `bridge: SIM (deterministic modelled fills)`, one per boot. No other MT5/broker line | journal |
| Kernel at boot | 3× `stage=PAPER fill_mode=MODELED … broker_reconcile=False` | journal lines 5, 14960, 25123 |
| `fills.fill_mode` | table empty: no value other than MODELED exists, because no value exists | SQL |
| `signals.execution_mode` | PAPER 205/205 (column and `market_snapshot`) | SQL |
| Non-PAPER `stage_events` | 900 rows (env or effective stage ≠ PAPER), **all** dated 2026-08-02/06/07 (iMac), in fixed per-run proportions that repeat across days (e.g. MICRO/MICRO 10·36·18, SCALED/SCALED 15·54·27) together with `DEMOTION_WRITTEN`. This matches `tests/test_stage.py`, `tests/test_stage_immutable.py`, `tests/test_promotion.py` parametrised over all stages. Frankfurt: exactly 3 rows, all BOOT PAPER/PAPER | SQL; test files |
| Secrets in git | no tracked `.env`/secret/key files; `git log --all -p` has 0 added lines assigning an MT5/broker password or login | git |

---

## 13. Test-residue census (`nexus_prodcopy`, nothing deleted)

Frankfurt era starts 2026-09-20 09:38 UTC. Everything before it came from `nexus_dev` on the iMac: a mix of real dev-soak rows and test rows.

| Table | Total | Future-dated (> now) | Pre-Frankfurt | Frankfurt | Provenance and how identified |
|---|---|---|---|---|---|
| `validator_log` | 4,544 | **688** | 2,144 | 1,712 (= 214 cycles × 8) | 688 rows at 2099-06-01 08:00, 86 × 8 rules, ids 10914–21792. Written by `tests/test_learning_loop.py:352,379` (`run_analysis_cycle` at 2099-06-01 08:00 with symbol `GC=F`). Its cleanup (`:96`) deletes only `TST_LL%`, so these rows survive. Pre-Frankfurt 2,144: mixed soak and test, not separable by symbol (all `GC=F`) — **hypothesis:** separable by matching `ts` to real `signals`/journal cycles |
| `stage_events` | 1,965 | 0 | 1,962 | 3 | 2026-08-02/06/07 bursts incl. MICRO/SHADOW/SCALED + DEMOTION_WRITTEN (test_stage*, test_promotion). At most a handful are real iMac boots |
| `kernel_events` | 4,840 | 0 | 4,840 | 0 | all 2026-08-02 → 08-07; production never called the kernel (Q4). 100% test |
| `doctrines` | 1,293 | 0 | 607 | 686 | Pre-Frankfurt includes test inserts (e.g. `tests/test_learning_loop.py:660-672` writes FABLE 4/6/8 + 2 PARSE_FALLBACK, rolled back when it works; exact-hour `ts` such as 2026-08-02 12:00). Frankfurt 686 are real |
| `signals` | 205 | 0 | 23 | 182 | 23 iMac rows (ids 7–1366). The id gap to 5314 is sequence burn by test inserts (F-34) |
| `state_vectors` | 340 | 0 | 123 | 217 | test cleanup by `ts ≥ 2099` works; none future-dated remain |
| `command_audit` | 26 | 0 | 11 | 15 | 11 are the 2026-08-07 iMac drill and deck tests |
| `candles` / `indicator_snapshots` / `macro_observations` / `etf_holdings` | 2,989 / 330 / 1,359 / 14 | 0 | 2,788 / 129 / 1,337 / 8 | 201 / 201 / 22 / 6 | market data. No residue found |
| `positions`, `fills`, `basis_readings`, `comex_stocks` | 0 | — | — | — | empty |
| `cot_reports`, `econ_events`, `news_articles`, `pod_stats`, `dim_rankings` | no `ts` column | — | — | — | test cleanups key on 2099 dates / `TST_` markers. Not censused row-by-row (hypothesis: clean. Confirm with `report_date`/`event_ts`/`published_at > now()` counts) |

**Safest identifier set for the future archive move (F-05):** `ts > now()` (validator_log), `kernel_events` wholesale, `stage_events` on 2026-08-02/06/07, and `doctrines`/`validator_log` rows before 2026-09-20 matched against a list of real soak cycles.

---

## 14. New findings (F-35 +)

**F-35 · HIGH · Ring 2 (fusion) · Every macro value read back from Postgres is thrown away as "missing"**
- **Problem:** `_num()` accepts only `int`/`float` (`fusion/regime.py:79-82`, `fusion/rag.py:76-81`). psycopg2 returns `numeric` columns as `decimal.Decimal`, so every value read from `state_vectors` becomes `None`. The regime HMM therefore sees **0 complete rows** forever (F-11), RULE5 is permanently SKIPPED, and every RAG embedding has all numeric dimensions flagged "absent".
- **Evidence:** read-only run against `nexus_prodcopy`: 340 rows, types `Decimal`; `regime.build_feature_matrix(rows)` → **0 rows**; the same rows cast to float → **338 rows** (enough to train; `REGIME_MIN_ROWS=168`, `config.py:232`). The latest embedding is `[0.5, 0.0, 0.5, 0.0, …]`. Only **18 distinct** vectors exist among 331 embeddings, and they differ only in categorical dimensions. `fusion/state_vector.py:68` has the same check but builds from in-memory floats, so it is unaffected. There is no `register_type`/Decimal adapter anywhere in the repo.
- **Recommendation:** accept `numbers.Real` / `Decimal` in both `_num`s (or register a psycopg2 `DECIMAL→float` caster in `core/database.py`). Add a DB round-trip test (insert → `load_history` → shape > 0). Re-embed via `python3 -m fusion.rag --backfill`. Treat RULE5 becoming live as a behaviour change and review it at PAPER first (as F-11 says).
- **Effort:** S · **Change risk:** Medium. The fix is one line, but it switches RULE5 on and changes RAG vectors. Neither file is 🔒.

**F-36 · HIGH · Ops / Ring 2 · Anthropic credits ran out 2026-09-25 12:06 UTC and nothing alerted**
- **Problem:** Since 2026-09-25 12:06:30 every analysis and doctrine call returns 400 "credit balance is too low". The system degraded safely (FLAT, no signals) but has been AI-blind for 4+ days. The heartbeat keeps reporting healthy and no alert was raised.
- **Evidence:** journal line 26332 (first error), 901 occurrences through 2026-09-29 10:23:35. Last HTTP 200 at 2026-09-25 11:53:26. Last signal #5495 11:53:08. 258 PARSE_FALLBACK + 40 EXPIRY_FALLBACK doctrines after 12:06 (SQL). Heartbeat `alive=10 … dead=0` on 2026-09-28 12:00:26. `ai/doctrine.py` and `ai/analysis.py` contain no Telegram/alert call (grep).
- **Recommendation:** classify API failures (billing 400, auth 401, overloaded 529, timeout). On the first billing/auth failure, send one Telegram alert, repeated daily. Expose "last successful model call" in `/status` and `ops_heartbeats`. Wire it into the external dead-man (F-12). Top up credits with auto-reload and set a spend alert in the Anthropic console.
- **Effort:** S · **Change risk:** Low (the alert path is additive; the doctrine path is 🔒, so hook the alert from `backend.py` or `ai/analysis.py`).

**F-37 · MEDIUM · Ring 2 · API outages are recorded as `PARSE_FALLBACK`**
- **Problem:** A failed model call or an exhausted budget is persisted with source `PARSE_FALLBACK` (`ai/doctrine.py:505-506`). Parse failures and billing outages are indistinguishable in `doctrines.source`. Every "fallback share" metric (Task 23 weekly, promotion pack) mixes them. The draft's "degraded safely (FLAT, `API_UNAVAILABLE`)" (F-20) cites a label that does not exist.
- **Evidence:** 258/258 post-outage PARSE_FALLBACK rows carry `no_trade_reason='model call failed or budget exhausted'` and `raw_response` NULL. Frankfurt had 0 genuine parse failures before the outage.
- **Recommendation:** additive 🔒 amendment: `SOURCE_API_UNAVAILABLE` (and optionally `BUDGET_EXHAUSTED`) at `ai/doctrine.py:505`. Until then, reports split PARSE_FALLBACK by `no_trade_reason`.
- **Effort:** S · **Change risk:** Low (label only; FLAT behaviour unchanged).

**F-38 · HIGH · Data integrity · Future-dated test rows poison every rolling window**
- **Problem:** 688 `validator_log` rows dated 2099-06-01 08:00 sit inside every `ts > now() − N` query, now and until 2099. That includes Q3, the weekly report and anything the promotion pack computes over validator decisions.
- **Evidence:** §13. Source `tests/test_learning_loop.py:352,379`; the cleanup at `:96` misses them (`symbol='GC=F'`, not `TST_LL%`).
- **Recommendation:** (a) archive the rows (F-05); (b) bound every reporting window on both sides (`ts <= now()`); (c) the test must clean up by its own `ts` or use a `TST_` symbol; (d) the conftest guard (F-33).
- **Effort:** S · **Change risk:** Low.

**F-39 · MEDIUM · Ring 1 · The paper engine fills on the still-forming H1 bar and stamps the fill with the bar's open time**
- **Problem:** `_latest_h1_bar` takes the newest candle, which is the in-progress bar (`exec_/paper_engine.py:289-303`). Its "close" is the live price at that moment, and its high/low include price action from *before* the signal existed. The module docstring says entry "is decided on the H1 CLOSE" (`:8`). `filled_at`/`opened_at` are the bar-open timestamp, not the fill time.
- **Evidence:** #5369 was persisted 01:28:32 and FILLED 01:28:45 (journal 8781) on bar 01:00. #5370 was persisted 01:58:50 and FILLED 01:59:01 on the same bar. 10/139 fills have `filled_at < ts`, and `filled_at` is always :00:00 (Q2).
- **Impact:** age-at-fill and trade timelines are wrong by up to 59 min (this matters for the Observatory). The fill model has look-ahead: exits can be triggered by highs/lows that pre-date the signal. Direction and size of the bias are a **hypothesis**; confirm by re-simulating the 139 fills on closed bars only.
- **Recommendation:** evaluate only closed bars (`ts < date_trunc('hour', now())`), stamp wall-clock fill time, and ignore the part of a bar before `signal.ts`. Subsumed by F-02 if the swing book moves to the router/SimBridge.
- **Effort:** S · **Change risk:** Low (PAPER only).

**F-40 · MEDIUM · Ring 2 / core · A signal can be persisted with no validator audit trail; the DB pool has no liveness check or timeout**
- **Problem:** When `validator_log` cannot be written, the validator logs and continues (`risk/validator.py:142-143`, 🔒, by design). The analyst then persists the signal anyway. The connection pool (`core/database.py:31`, `SimpleConnectionPool(1, 10, dsn=…)`) has no pre-ping, keepalive or `connect_timeout`, which is a gap against INVARIANT 6.
- **Evidence:** signal #5415 (2026-09-23 06:42:08, 3 s after a restart) has **0** `validator_log` rows. Journal 15020–15038: `psycopg2.OperationalError: server closed the connection unexpectedly` → "could not write validator_log rows; continuing" → `SIGNAL_PERSISTED id=5415`. It is the only one of 182 without audit rows. Why the server closed the connection is a hypothesis (Postgres restarted with the service; confirm in the Postgres log).
- **Recommendation:** in `ai/analysis.py` (not 🔒), refuse to persist a signal when its cycle's audit rows are absent (check by `ts`), outcome `AUDIT_WRITE_FAILED`. In `core/database.py`, add `connect_timeout`, TCP keepalives and a `SELECT 1` pre-ping on checkout.
- **Effort:** S · **Change risk:** Low (restrictive).

**F-41 · MEDIUM · Ops · What runs on Frankfurt is not what the repo's deploy kit describes, and restarts leave no operator record**
- **Problem:** The running service uses a venv inside the checkout, while the repo unit points outside it. So the server's unit and/or layout drifted from `ops/deploy/`. Two manual restarts (09-23, 09-25) happened with no record of who or why. The draft's author did not know about the second one.
- **Evidence:** 1,808 journal paths `/home/nexus/nexus/venv/lib/python3.12/site-packages`; `ops/deploy/nexus.service:42-44` expects `/home/nexus/venv`. Restarts: journal 14949 and 25112.
- **Recommendation:** `systemctl cat nexus` on the server, diffed against the repo unit, then reinstall from the repo (this closes F-28). Keep a one-line operator change log (date, action, reason) for every stop, restart or deploy. The promotion pack reads it.
- **Effort:** S · **Change risk:** Low (a unit change needs a planned restart).

**F-42 · MEDIUM · Tests · Fixtures take whole-table locks and delete every row, relying on rollback**
- **Problem:** `tests/test_learning_loop.py:68` (`DELETE FROM signals WHERE … NOT LIKE marker`), `:427-429` (`DELETE FROM pod_stats/fills/positions`) and `:669` (`DELETE FROM doctrines`) are safe only because the transaction is rolled back and no code under test commits on that connection (verified: no `.commit()` in `fusion/learning_loop.py`, `fusion/rag.py`, `fusion/regime.py`). Against a live DB they would lock the trading tables for the test's duration. A single future `commit()` in the code under test would wipe them.
- **Recommendation:** the F-33 conftest guard first. Then scope the deletes to test markers, or run these tests in a throwaway schema.
- **Effort:** S · **Change risk:** Low.

**F-43 · LOW · Data · `signals` and `validator_log` have no `ts` index**
- **Evidence:** `pg_indexes`: `ts` indexes exist for doctrines, fills, kernel_events, stage_events, command_audit, state_vectors, basis_readings, comex_stocks, etf_holdings. `signals` and `validator_log` have only their primary keys.
- **Recommendation:** `CREATE INDEX CONCURRENTLY` on `(ts)` for both, via an approved migration, before the Observatory polls them (Appendix B.2 already plans this).
- **Effort:** S · **Change risk:** Low.

**F-44 · LOW · Ring 3 · `cot_reports`, `econ_events` and `news_articles` have no `ts` column**
- **Problem:** Appendix B defines sensor freshness as "latest `ts` per table". These three tables use `fetched_at` (plus `report_date`/`event_ts`/`published_at`).
- **Recommendation:** the projector maps freshness per table to `fetched_at`. No migration needed.
- **Effort:** S · **Change risk:** None.

**F-45 · LOW · Process · The draft plan is untracked, and the evidence window moved**
- **Problem:** `docs/architect/NEXUS_IMPROVEMENT_PLAN.md` is untracked in git (`git status`: `?? docs/`). The brief describes the prod copy as ≈ 2026-09-23, but it is 2026-09-29.
- **Recommendation:** commit the draft alongside this verified version when the architect approves. State the snapshot timestamp in every audit brief.
- **Effort:** S · **Change risk:** None.

---

## 15. App readiness (Appendix B against `nexus_prodcopy`)

**Tables:** all 13 tables Appendix B reads exist: `validator_log, doctrines, signals, positions, fills, kernel_events, stage_events, command_audit, state_vectors, macro_observations, cot_reports, econ_events, news_articles`. **Missing: `ops_heartbeats` only** (as expected).

| Need (Appendix B) | Columns present | Status |
|---|---|---|
| Rule decisions per cycle | `validator_log(id, ts, symbol, rule_name, rule_result, details, created_at)` | ✔ (no `ts` index, F-43; 688 future rows, F-38) |
| Posture, raw response, source, horizon | `doctrines(id, ts, regime, bias, conviction, risk_multiplier, enabled_pods, swing_signals_allowed, no_trade_reason, review_horizon_min, state_vector_id, source, raw_response, created_at)` | ✔ (source conflates API failure with parse failure, F-37) |
| Signal lifecycle with R/MAE/MFE | `signals(… status, filled_at, outcome_r, outcome_pips, mae_r, mfe_r, outcome_ts, lots, state_vector_id …)` | ✔ (`filled_at` = bar open, F-39) |
| Reserve → send → fill → close | `positions(client_order_id, source, pod, direction, lots, entry_px, stop_px, tp1_px, tp2_px, state, opened_at, closed_at, realized_pnl_usd, close_reason)`; `fills(ts, client_order_id, signal_id, requested_px, fill_px, slippage, spread_at_send, fill_mode, status, kernel_reason)` | ✔ columns. Both tables are empty. `positions` has one `state` column, not per-step timestamps |
| Kernel decisions with context | `kernel_events(id, ts, breaker, action, reason, context)` | ✔ (100% test residue today) |
| Governance and commands | `stage_events(id, ts, event, env_stage, effective_stage, reason)`; `command_audit(id, ts, chat_id, command, args, stage, outcome, detail)` | ✔ |
| Sensor freshness | `state_vectors.ts`, `macro_observations.ts/fetched_at` ✔; `cot_reports`, `econ_events`, `news_articles` have `fetched_at`, no `ts` | ⚠ F-44 |
| Process health | `ops_heartbeats` | ✘ missing, as planned |

**Correlation key: do an analysis cycle's `validator_log` rows and its `signals` row share the exact same `ts`? — YES.** 181 of 182 Frankfurt signals have exactly 8 `validator_log` rows with an identical `ts` (to the microsecond). The one exception, #5415, has none because the audit write failed (F-40). The other 33 of 214 distinct cycle timestamps are gate-blocked cycles (RULE0/RULE1 WAIT) with no signal, as expected. Examples:

| Signal | `ts` (shared) | `validator_log` rows | Results |
|---|---|---|---|
| #5314 | 2026-09-20 09:38:05.097203+00 | 8 | PASS, SKIPPED_NO_DATA (RULE5) |
| #5400 | 2026-09-22 20:39:35.283903+00 | 8 | PASS, SKIPPED_NO_DATA (RULE5) |
| #5495 | 2026-09-25 11:53:08.324891+00 | 8 | PASS, SKIPPED_NO_DATA (RULE5) |

"Doctrine in force at *t*" (latest doctrine with `ts ≤ t`, expired when `t ≥ ts + review_horizon_min`) also works as specified. §3.2 computes it for all 182 signals.
