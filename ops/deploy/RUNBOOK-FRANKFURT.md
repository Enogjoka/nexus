# RUNBOOK — moving the NEXUS brain to Frankfurt

You execute this by hand, in order. Each step ends with a **Report back** line:
paste exactly that to the architect before moving on. If any command does not
produce what the step says to expect, **stop and report** — do not improvise a
fix on a machine that is about to run a trading system.

Placeholders you substitute everywhere: `<server>` (the VPS address),
`<admin>` (your sudo login on it), `<you>` (your GitHub account).

---

## 0. Before you start

**Preconditions — all four must be true:**

1. `task/f1-frankfurt` is merged into `main`. The clone in step (d) must
   contain `ops/deploy/`.
2. A Frankfurt VPS running **Ubuntu 24.04 LTS** (it ships Python 3.12 and
   PostgreSQL 16), at least 2 GB RAM and 20 GB disk, reachable by SSH as `<admin>`.
3. **Anthropic credits are topped up.** The audit of 2026-09-19 found the
   balance exhausted since before 2026-08-07. Without credits, step (g) will
   never show a FABLE doctrine. Check at console.anthropic.com → Billing.
4. Your phone has Telegram with the NEXUS bot chat open.

**What moves and what does not.**
The corpus moves; the clocks do not reset. Every table in `nexus_dev` is
restored into the server's `nexus` database. Two honest caveats:

- `nexus_dev` was shared by the soak **and the test suite** until today. Its
  audit tables (`stage_events`, `kernel_events`, `doctrines`, `validator_log`)
  contain test-generated rows beside real ones, and they move with everything
  else. Whether to clean them is an architect decision. Do not improvise it
  during the migration.
- The system has not run since 2026-08-08. The promotion soak clock restarts
  at the first Frankfurt boot however much history moves, and the last kill
  drill (2026-08-07) is already past its 30-day window.

**Rollback at any point before step (i):** `sudo systemctl disable --now nexus`
on the server. The iMac keeps `nexus_dev` untouched plus the dump from step (c).

---

## (a) Server: user, Python, Postgres, clock, firewall

```bash
ssh <admin>@<server>

# a.1 — system packages
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3 python3-venv python3-dev build-essential \
    postgresql postgresql-client libpq-dev git openssl

# a.2 — Python must be 3.12 or newer
python3 --version

# a.3 — the service account: a system user with a home, no login shell
sudo adduser --system --group --home /home/nexus nexus

# a.4 — clock: UTC, NTP-synchronised. Every staleness and skew check in
#       NEXUS assumes a correct clock.
sudo timedatectl set-timezone UTC
timedatectl | grep -E "Time zone|synchronized"

# a.5 — firewall: SSH only. NEXUS listens on nothing, and the link port
#       (8765) stays closed until the WireGuard work exists.
sudo ufw allow OpenSSH
sudo ufw --force enable
sudo ufw status
```

Expect: `Python 3.12.x`, `Time zone: Etc/UTC`, `System clock synchronized: yes`,
and ufw `Status: active` allowing only OpenSSH.

**Report back:** the output of a.2, a.4 and a.5.

---

## (b) Server: database and role

Authentication is **peer over the local socket**: the OS user `nexus` *is* the
database role `nexus`. There is no database password to create, store or leak.

```bash
# b.1 — the role (no superuser, no password) and its database
sudo -u postgres createuser nexus
sudo -u postgres createdb --owner=nexus nexus

# b.2 — prove peer auth works for the service user
sudo -u nexus psql -d nexus -c "SELECT current_user, current_database(), version();"
```

Expect: `nexus | nexus | PostgreSQL 16.x …`

> **FUTURE, not now:** pgvector is installable here
> (`sudo apt install postgresql-16-pgvector`). Do **not** install it. The RAG
> stores embeddings as JSONB by design (Task 11), and adopting pgvector is a
> future task with its own brief.

**Report back:** the b.2 output.

---

## (c) iMac → server: move the corpus

### c.1 — iMac: bring Postgres back

It has refused to start since the 2026-09-19 reboot. A stale `postmaster.pid`
names PID 1020, and the OS reused that PID for another program.

```bash
pgrep -x postgres          # MUST print nothing. If it prints a PID, STOP and report.
rm /usr/local/var/postgresql@15/postmaster.pid
brew services restart postgresql@15
pg_isready                 # expect: /tmp:5432 - accepting connections
```

### c.2 — iMac: confirm nothing is writing

```bash
pgrep -f backend.py        # MUST print nothing (it has not run since 2026-08-08)
```

### c.3 — iMac: snapshot the row counts

Keep this output. Step (c.6) and the preflight must reproduce these numbers
exactly.

```bash
psql nexus_dev -c "
SELECT 'candles' AS t, count(*) FROM candles UNION ALL
SELECT 'state_vectors', count(*) FROM state_vectors UNION ALL
SELECT 'signals', count(*) FROM signals UNION ALL
SELECT 'doctrines', count(*) FROM doctrines UNION ALL
SELECT 'stage_events', count(*) FROM stage_events UNION ALL
SELECT 'positions', count(*) FROM positions UNION ALL
SELECT 'fills', count(*) FROM fills UNION ALL
SELECT 'pod_stats', count(*) FROM pod_stats UNION ALL
SELECT 'econ_events', count(*) FROM econ_events UNION ALL
SELECT 'schema_migrations', count(*) FROM schema_migrations;"
```

### c.4 — iMac: dump, checksum, copy

The dump holds no secrets: configuration never lives in the database
(INVARIANT 7). Keep the iMac copy. It is your rollback point.

```bash
pg_dump --format=custom --no-owner --no-privileges \
        --dbname=nexus_dev --file="$HOME/nexus_dev.dump"
shasum -a 256 "$HOME/nexus_dev.dump"
scp "$HOME/nexus_dev.dump" <admin>@<server>:/tmp/nexus_dev.dump
```

### c.5 — server: verify and restore

```bash
sha256sum /tmp/nexus_dev.dump      # MUST equal the iMac's shasum exactly
sudo chown nexus:nexus /tmp/nexus_dev.dump
sudo -u nexus pg_restore --no-owner --no-privileges --dbname=nexus /tmp/nexus_dev.dump
sudo -u nexus psql -d nexus -c "ANALYZE;"
```

An error mentioning the `public` schema or a `COMMENT` is harmless. **Any error
naming a table, a row or a constraint is not** — stop and report it.

### c.6 — server: the same counts

Run the c.3 query again on the server, against `nexus`:

```bash
sudo -u nexus psql -d nexus -c "
SELECT 'candles' AS t, count(*) FROM candles UNION ALL
SELECT 'state_vectors', count(*) FROM state_vectors UNION ALL
SELECT 'signals', count(*) FROM signals UNION ALL
SELECT 'doctrines', count(*) FROM doctrines UNION ALL
SELECT 'stage_events', count(*) FROM stage_events UNION ALL
SELECT 'positions', count(*) FROM positions UNION ALL
SELECT 'fills', count(*) FROM fills UNION ALL
SELECT 'pod_stats', count(*) FROM pod_stats UNION ALL
SELECT 'econ_events', count(*) FROM econ_events UNION ALL
SELECT 'schema_migrations', count(*) FROM schema_migrations;"

sudo rm /tmp/nexus_dev.dump        # the verified restore is the server's copy now
```

Every number must match c.3 exactly.

**Report back:** both checksums, then the c.3 and c.6 tables side by side.

---

## (d) Code: create the private remote, then clone

The repository has **no remote today** (`git remote -v` prints nothing), so
the remote is created first. History was checked on 2026-09-19: `.env` has
never been committed, and no secret-shaped string appears in any commit.

### d.1 — iMac: create the remote and push `main`

1. On github.com: **New repository** → name `nexus` → **Private** → no README,
   no .gitignore, no licence.
2. Then:

```bash
cd ~/Desktop/nexus
git checkout main
git remote -v                       # still empty — expected
git ls-files | grep -E '^\.env$'    # MUST print nothing
git remote add origin git@github.com:<you>/nexus.git
git push -u origin main
git remote -v                       # now shows origin
```

### d.2 — server: a read-only deploy key for the `nexus` user

```bash
sudo -u nexus -H mkdir -m 700 /home/nexus/.ssh
sudo -u nexus -H ssh-keygen -t ed25519 -N "" -C "nexus-frankfurt-deploy" \
    -f /home/nexus/.ssh/id_ed25519
sudo -u nexus -H cat /home/nexus/.ssh/id_ed25519.pub
```

On github.com: repo → **Settings** → **Deploy keys** → **Add deploy key** →
paste the line above → leave **Allow write access UNCHECKED**. The server can
pull the code but never rewrite it.

```bash
sudo -u nexus -H ssh -o StrictHostKeyChecking=accept-new -T git@github.com
```

Expect `…successfully authenticated…`. Compare the fingerprint it printed with
GitHub's published list at
https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints.
If they differ, stop.

### d.3 — server: clone

```bash
sudo -u nexus -H git clone git@github.com:<you>/nexus.git /home/nexus/nexus
sudo -u nexus -H git -C /home/nexus/nexus log --oneline -1
ls /home/nexus/nexus/ops/deploy/    # nexus.service  preflight.py  RUNBOOK-FRANKFURT.md
```

### d.4 — the environment: pinned to the versions that passed on the iMac

`requirements.txt` is unpinned. The versions that passed 894 tests on the iMac
are the versions that ship, so they are frozen into a constraints file.

```bash
# on the iMac
python3 -m pip freeze > "$HOME/nexus-constraints.txt"
scp "$HOME/nexus-constraints.txt" <admin>@<server>:/tmp/nexus-constraints.txt
```

```bash
# on the server
sudo chown nexus:nexus /tmp/nexus-constraints.txt
sudo -u nexus -H python3 -m venv /home/nexus/venv
sudo -u nexus -H /home/nexus/venv/bin/pip install --upgrade pip
sudo -u nexus -H /home/nexus/venv/bin/pip install \
    -r /home/nexus/nexus/requirements.txt -c /tmp/nexus-constraints.txt
sudo -u nexus -H /home/nexus/venv/bin/pip check
```

If pip refuses a pinned version, **stop and report it**. Do not loosen a pin
to make the install succeed.

**Report back:** the `git log --oneline -1` from d.3 and the `pip check` output.

---

## (e) Server: the `.env`, typed by hand

Create it with owner-only permissions **before** a single secret goes in:

```bash
sudo -u nexus -H bash -c 'umask 077 && touch /home/nexus/nexus/.env'
```

Generate the link secret **on the server**, straight into the file. The value
never appears on screen or in your shell history:

```bash
sudo -u nexus -H bash -c \
  'printf "HMAC_SECRET=%s\n" "$(openssl rand -hex 32)" >> /home/nexus/nexus/.env'
```

Then open the file and type in the rest:

```bash
sudo -u nexus -H nano /home/nexus/nexus/.env
```

**Format rule.** One `KEY=value` per line. No quotes, no `export`, no spaces
around `=`, no trailing comments. Both systemd and python-dotenv read this
file, and they only agree on plain lines.

| Key | Where you find the value |
|---|---|
| `NEXUS_STAGE` | Not a secret. It is `PAPER`. The migration lands at the bottom rung, always. |
| `DATABASE_URL` | Not a secret. `postgresql:///nexus?host=/var/run/postgresql`: the socket from step (b), with its directory named explicitly because the bundled libpq may look in `/tmp`. |
| `ANTHROPIC_API_KEY` | console.anthropic.com → **API Keys** → create a **new** key named `nexus-frankfurt`, so the iMac's key can be revoked on its own. |
| `FRED_API_KEY` | fredaccount.stlouisfed.org → **API Keys**. The iMac's key works; a new one is cleaner. |
| `TELEGRAM_BOT_TOKEN` | Telegram **@BotFather** → `/mybots` → your NEXUS bot → **API Token**. Same bot as before. |
| `TELEGRAM_CHAT_IDS` | Your chat id. Copy it from the iMac's `.env`. |
| `HMAC_SECRET` | Already written by the command above. Never copied from anywhere. The Windows exec box will need this same value when it joins; carry it across by hand then. |

`OPENAI_API_KEY`, `GEMINI_API_KEY` and `FINNHUB_API_KEY` are defined in
`config.py` but no agent reads them. Leave them out.

```bash
ls -l /home/nexus/nexus/.env        # MUST show -rw------- nexus nexus
sudo -u nexus -H grep -oE '^[A-Z_]+=' /home/nexus/nexus/.env   # names only, never values
```

**Report back:** the `ls -l` line and the list of key names. **Never** paste a
value.

---

## (f) Server: preflight — must pass

```bash
sudo -u nexus -H bash -c \
  'cd /home/nexus/nexus && /home/nexus/venv/bin/python ops/deploy/preflight.py'
```

It is read-only. It checks the Python version, compiles every module on this
interpreter, checks that the stage is PAPER, that every required key is SET
(values are never shown), and that every runtime package imports. It then
confirms the database connects in a read-only session, all 16 migrations are
applied, the corpus arrived, and disk space is sufficient. Its `corpus restored`
line prints the same counts as c.3; compare them.

It must end with `PREFLIGHT PASSED`. On `PREFLIGHT FAILED`, fix exactly the
named check and run it again.

> **Never run the test suite on this server.** The suite writes to whatever
> `DATABASE_URL` names, and here that is production. Task 22.1 exists because
> test rows were once read back as real trades.

**Report back:** the full preflight table.

---

## (g) Server: install, enable, watch the boot

```bash
sudo cp /home/nexus/nexus/ops/deploy/nexus.service /etc/systemd/system/nexus.service
sudo systemd-analyze verify /etc/systemd/system/nexus.service   # MUST print nothing
sudo systemctl daemon-reload
sudo systemctl enable --now nexus
sudo systemctl status nexus --no-pager
journalctl -u nexus -f
```

Watch for, in order:

1. `NEXUS starting - stage=PAPER`
2. `migrations: schema already up to date`
3. `doctrine: pod stats provider wired to the learning loop`
4. `starting 16 agents`
5. `telegram: command deck polling every 30s (two-step confirm 60s)`
6. `heartbeat: alive=10 registered=6 dead=0 (of 16 agents)`
7. The first **FABLE** doctrine: `doctrine: issued bias=… source=FABLE`.
   On a cold AppState, Fable may choose `FLAT`. `source=FABLE` is what proves
   the credits and the key work.

Leave the tail with Ctrl-C, then confirm from the stored journal:

```bash
journalctl -u nexus --since "15 min ago" --no-pager \
  | grep -E "stage=PAPER|starting 16 agents|heartbeat: alive|source=FABLE"
```

**Expected noise, not failures:** `XAUUSD=X` 404s from the basis sensor every
5 minutes (no spot feed exists; known), 403s from some news feeds, and
`fetch_gld_tonnes … returning None`.

**Stop and report** on any of these: `dead=` above 0 that persists across two
heartbeats; `credit balance is too low` (top up, then
`sudo systemctl restart nexus`); any `CRITICAL`; or the unit entering `failed`.
Five crashes in ten minutes leave it down **on purpose**, until a human has
read why.

**Report back:** the grep output, including the first `source=FABLE` line.

---

## (h) Phone: `/status` — the acceptance

Send `/status` to the NEXUS bot. Allow up to 30 seconds; the bot polls.

Expect a reply starting `NEXUS status` with `stage: PAPER`, and an `uptime:`
line counted in minutes. That fresh uptime is how you know Frankfurt answered,
not a memory of the iMac.

**The remote system answering your pocket is the acceptance.**

**Report back:** the reply text.

Recommended within 30 days, because the promotion pack requires it: re-run
the kill drill against Frankfurt (`/killswitch` → `CONFIRM`, watch the journal
halt, `/clearkill` → `CONFIRM`, then `sudo systemctl restart nexus`).

---

## (i) iMac: stand down

```bash
pgrep -f backend.py        # MUST print nothing — and it must never again run
                           # with the production bot token or production key
```

Then, in the iMac's `~/Desktop/nexus/.env`, **delete the `TELEGRAM_BOT_TOKEN`
line**. Two processes polling one bot token fight over every update
(Telegram answers `409 Conflict`), and a dev process must never answer your
phone. If the iMac needs Telegram again, give it its own bot. Give it its own
Anthropic key too, or none: the tests mock the API.

`nexus_dev` stays exactly where it is. From this line on it is the
**development** database.

---

**Production truth lives in Frankfurt. The iMac tests against its own data. The two never sync casually.**

---

### Appendix — day-2 operations on the server

| Task | Command |
|---|---|
| Status | `sudo systemctl status nexus --no-pager` |
| Live log | `journalctl -u nexus -f` |
| Today's log | `journalctl -u nexus --since today --no-pager` |
| Clean stop (stays stopped) | `sudo systemctl stop nexus` |
| Restart after a kill-switch halt | `sudo systemctl restart nexus` |
| Kill switch without Telegram | `sudo -u nexus touch /home/nexus/nexus/KILL` (the kernel sees it within 30 s) |
| Recover from a crash-loop lockout | read the journal first, then `sudo systemctl reset-failed nexus && sudo systemctl start nexus` |
| Update the code | `sudo -u nexus -H git -C /home/nexus/nexus pull --ff-only`, then re-run step (f), then `sudo systemctl restart nexus` |
