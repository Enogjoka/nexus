#!/usr/bin/env python3
"""
NEXUS server preflight — run on the server BEFORE the systemd unit is enabled.

    sudo -u nexus -H bash -c 'cd /home/nexus/nexus && /home/nexus/venv/bin/python ops/deploy/preflight.py'

Exit 0 means every blocking check passed. Exit 1 means do not install the
service. The runbook (ops/deploy/RUNBOOK-FRANKFURT.md, step f) treats a failed
preflight as a hard stop: fix the named check and run it again.

READ-ONLY, AND THAT CONSTRAINS WHAT IT MAY IMPORT.
This script writes nothing: no files, no rows, no __pycache__. That rules out
importing the application itself. `import backend` pulls in exec_/router,
which imports risk.stage, and risk.stage INSERTS a BOOT row into stage_events
at import time — so merely checking that the backend imports would forge a
boot record in the production audit trail. The source is instead compiled in
memory, and the database is opened in a read-only session. The only
application module imported is config, which is pure constants.

SECRETS ARE NEVER PRINTED. Environment keys are reported as SET or MISSING —
never a value, never a length. Every message that reaches the screen passes
through _redact(), which masks the value of every key named in
config.PREFLIGHT_REQUIRED_ENV / PREFLIGHT_OPTIONAL_ENV, because some library
errors quote the connection string they failed on.

THE MIGRATION LANDS AT PAPER, ALWAYS. The effective stage is computed exactly
as config.py computes it, and anything other than
config.PREFLIGHT_REQUIRED_STAGE fails the preflight. Moving to a new machine
is not an occasion to climb the ladder.
"""
import importlib
import os
import re
import shutil
import stat
import sys
from pathlib import Path

# Read-only includes the checkout: importing config would otherwise drop a
# config.*.pyc into the repo's __pycache__. Set before any application import.
sys.dont_write_bytecode = True

# Runnable as a plain script from any working directory: the repository root
# is two levels above this file (ops/deploy/preflight.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
INFO = "INFO"

DB_CONNECT_TIMEOUT_SECONDS = 5

# requirements.txt names that differ from their import names, and entries
# that are not needed at runtime. requirements.txt stays the single source of
# truth for WHAT is required; this only translates the spelling.
_IMPORT_NAME = {"psycopg2-binary": "psycopg2", "python-dotenv": "dotenv"}
_NOT_RUNTIME = {"pytest"}

# Tables whose row counts prove the corpus arrived. Printed so the operator
# can compare them against the iMac snapshot taken in runbook step (c).
_CORPUS_TABLES = (
    "candles", "state_vectors", "signals", "doctrines", "stage_events",
    "positions", "fills", "pod_stats", "econ_events",
)

_secret_values = []


def _redact(text) -> str:
    """Mask every known secret value that appears in `text`."""
    text = str(text)
    for value in _secret_values:
        if value:
            text = text.replace(value, "***")
    return text


class Report:
    def __init__(self):
        self.rows = []

    def add(self, check, result, detail=""):
        self.rows.append((check, result, _redact(detail)))

    @property
    def failed(self):
        return [r for r in self.rows if r[1] == FAIL]

    def render(self) -> str:
        width = max(len(r[0]) for r in self.rows) if self.rows else 10
        lines = ["", "NEXUS preflight — " + str(REPO_ROOT), "=" * 72]
        for check, result, detail in self.rows:
            lines.append(f"{check:<{width}}  {result:<4}  {detail}")
        lines.append("=" * 72)
        if self.failed:
            lines.append(
                f"PREFLIGHT FAILED — {len(self.failed)} check(s) failed. "
                "Do not install the service."
            )
        else:
            lines.append("PREFLIGHT PASSED — the server may run NEXUS at PAPER.")
        return "\n".join(lines)


# ==========================================================================
# checks
# ==========================================================================


def check_not_root(report: Report) -> None:
    """The service runs as `nexus`; so should the check that vouches for it."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        report.add("run as service user", WARN,
                   "running as root — run as `nexus` so file checks see what the service sees")
    else:
        report.add("run as service user", PASS, f"uid {os.getuid() if hasattr(os, 'getuid') else '?'}")


def check_env_file(report: Report) -> Path:
    path = REPO_ROOT / ".env"
    if not path.exists():
        report.add(".env present", FAIL, f"{path} not found (runbook step e)")
        return path
    if not os.access(path, os.R_OK):
        report.add(".env present", FAIL, f"{path} exists but is not readable by this user")
        return path

    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        report.add(".env permissions", FAIL,
                   f"mode {oct(mode)} — group/other can read secrets; run: chmod 600 {path}")
    else:
        report.add(".env permissions", PASS, oct(mode))
    return path


def load_env(report: Report, path: Path) -> None:
    """
    Load .env the way backend.py does (python-dotenv, never overriding values
    already in the environment). If python-dotenv is missing, the packages
    check will fail on it; the environment may still be supplied by the shell.
    """
    try:
        from dotenv import load_dotenv
    except Exception:
        report.add("load .env", WARN, "python-dotenv not importable; using the shell environment only")
        return
    if path.exists():
        load_dotenv(path, override=False)
        report.add("load .env", PASS, "loaded (existing environment values take precedence)")


def import_config(report: Report):
    """
    Import config.py — pure constants, no side effects. If it will not import,
    nothing else can be trusted, so the caller stops.
    """
    try:
        return importlib.import_module("config")
    except ValueError as exc:
        raw = os.environ.get("NEXUS_STAGE")
        report.add("config imports", FAIL,
                   f"config.py rejected NEXUS_STAGE={raw!r} ({exc}). Set NEXUS_STAGE=PAPER.")
    except Exception as exc:
        report.add("config imports", FAIL, f"{type(exc).__name__}: {exc}")
    return None


def register_secrets(config) -> None:
    for key in list(config.PREFLIGHT_REQUIRED_ENV) + list(config.PREFLIGHT_OPTIONAL_ENV):
        value = os.environ.get(key)
        if value:
            _secret_values.append(value)
            # A DSN's password component can surface on its own in an error.
            if key == "DATABASE_URL":
                match = re.match(r"^[a-z0-9+]+://[^:/@]*:([^@]+)@", value)
                if match:
                    _secret_values.append(match.group(1))


def check_python(report: Report, config) -> None:
    need = tuple(config.PREFLIGHT_MIN_PYTHON)
    have = sys.version_info[:3]
    label = ".".join(map(str, have))
    if have[:2] >= need:
        report.add("python version", PASS, f"{label} (need >= {'.'.join(map(str, need))})")
    else:
        report.add("python version", FAIL,
                   f"{label} is older than {'.'.join(map(str, need))} (runbook step a)")


def check_source_compiles(report: Report) -> None:
    """
    Compile every application module IN MEMORY on this interpreter.

    The code was developed on a newer Python than the server runs; a syntax
    feature this interpreter lacks would otherwise surface as a crash-loop
    after the service starts. compile() writes no .pyc, so this stays
    read-only. Tests are skipped — they never run on the server.
    """
    failures = []
    count = 0
    for path in sorted(REPO_ROOT.rglob("*.py")):
        parts = path.relative_to(REPO_ROOT).parts
        if parts[0] in ("tests", ".venv", "venv", ".git", ".claude") or "__pycache__" in parts:
            continue
        count += 1
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as exc:
            failures.append(f"{path.relative_to(REPO_ROOT)}:{exc.lineno} {exc.msg}")
    if failures:
        report.add("source compiles", FAIL, "; ".join(failures[:3]))
    else:
        report.add("source compiles", PASS, f"{count} modules on {sys.version.split()[0]}")


def check_stage(report: Report, config) -> None:
    raw = os.environ.get("NEXUS_STAGE")
    effective = config.get_stage().value
    required = config.PREFLIGHT_REQUIRED_STAGE
    if effective != required:
        report.add("stage is PAPER", FAIL,
                   f"effective stage is {effective}. A migration lands at {required}, always.")
    elif raw is None:
        report.add("stage is PAPER", PASS,
                   "NEXUS_STAGE unset — config defaults to PAPER (set it explicitly)")
    else:
        report.add("stage is PAPER", PASS, f"NEXUS_STAGE={raw}")


def check_env_keys(report: Report, config) -> None:
    missing = [k for k in config.PREFLIGHT_REQUIRED_ENV if not os.environ.get(k)]
    present = [k for k in config.PREFLIGHT_REQUIRED_ENV if os.environ.get(k)]
    if missing:
        report.add("required env keys", FAIL,
                   f"MISSING or EMPTY: {', '.join(missing)} (runbook step e)")
    else:
        report.add("required env keys", PASS, f"all {len(present)} SET: {', '.join(present)}")

    optional = [k for k in config.PREFLIGHT_OPTIONAL_ENV if os.environ.get(k)]
    report.add("optional env keys", INFO,
               f"SET: {', '.join(optional) or 'none'} — no agent reads these today")


def check_packages(report: Report) -> None:
    requirements = REPO_ROOT / "requirements.txt"
    if not requirements.exists():
        report.add("packages import", FAIL, "requirements.txt not found")
        return

    names = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        dist = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip().lower()
        if dist and dist not in _NOT_RUNTIME:
            names.append(_IMPORT_NAME.get(dist, dist.replace("-", "_")))

    broken = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:
            broken.append(f"{name} ({type(exc).__name__})")
    if broken:
        report.add("packages import", FAIL,
                   f"{', '.join(broken)} — run the pip install in runbook step d")
    else:
        report.add("packages import", PASS, f"{len(names)} runtime packages")


def check_database(report: Report) -> None:
    """
    Connect with a timeout (INVARIANT 6) in a READ-ONLY session, then verify
    migrations and the corpus. Never runs a migration: pending migrations mean
    the restore in runbook step (c) did not complete, and that is exactly what
    this check exists to catch.
    """
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        report.add("database connects", FAIL, "DATABASE_URL is not set")
        return

    try:
        import psycopg2
    except Exception:
        report.add("database connects", FAIL, "psycopg2 not importable")
        return

    try:
        conn = psycopg2.connect(dsn, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS)
    except Exception as exc:
        report.add("database connects", FAIL, f"{type(exc).__name__}: {exc}".strip())
        return

    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, "
                        "current_setting('server_version')")
            db, user, version = cur.fetchone()
        report.add("database connects", PASS, f"db={db} role={user} postgres {version}")

        check_migrations(report, conn)
        check_corpus(report, conn)
    except Exception as exc:
        report.add("database checks", FAIL, f"{type(exc).__name__}: {exc}".strip())
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass


def check_migrations(report: Report, conn) -> None:
    files = sorted(p.name for p in (REPO_ROOT / "migrations").glob("*.sql"))
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
        if not cur.fetchone()[0]:
            report.add("migrations current", FAIL,
                       "schema_migrations absent — the database was never restored (runbook step c)")
            return
        cur.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}

    pending = [f for f in files if f not in applied]
    unknown = sorted(applied - set(files))
    if pending:
        report.add("migrations current", FAIL,
                   f"{len(pending)} not applied ({', '.join(pending[:3])}"
                   f"{'…' if len(pending) > 3 else ''}) — restore incomplete, or the "
                   "checkout is newer than the dump")
    elif unknown:
        report.add("migrations current", FAIL,
                   f"database has migrations this checkout lacks ({', '.join(unknown[:3])}) — "
                   "the clone is older than the corpus; pull main")
    else:
        report.add("migrations current", PASS, f"all {len(files)} applied")


def check_corpus(report: Report, conn) -> None:
    """
    The corpus moves; the clocks do not reset. An empty candles table after a
    'successful' migration means a fresh database was set up instead of the
    real one restored — and every learning clock would silently start at zero.
    """
    counts = {}
    with conn.cursor() as cur:
        for table in _CORPUS_TABLES:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
            if not cur.fetchone()[0]:
                counts[table] = None
                continue
            cur.execute(f"SELECT count(*) FROM {table}")
            counts[table] = int(cur.fetchone()[0])

    summary = " ".join(
        f"{t}={'absent' if n is None else n}" for t, n in counts.items()
    )
    if not counts.get("candles"):
        report.add("corpus restored", FAIL,
                   f"candles is empty — the corpus did not move (runbook step c). {summary}")
    else:
        report.add("corpus restored", PASS, summary)


def check_disk(report: Report, config) -> None:
    free_gb = shutil.disk_usage(REPO_ROOT).free / (1024 ** 3)
    need = float(config.PREFLIGHT_MIN_FREE_GB)
    if free_gb < need:
        report.add("disk space", FAIL, f"{free_gb:.1f} GB free, need >= {need:.0f} GB")
    else:
        report.add("disk space", PASS, f"{free_gb:.1f} GB free (need >= {need:.0f} GB)")


def main() -> int:
    report = Report()
    check_not_root(report)
    env_path = check_env_file(report)
    load_env(report, env_path)

    config = import_config(report)
    if config is None:
        print(report.render())
        return 1
    register_secrets(config)

    check_python(report, config)
    check_source_compiles(report)
    check_stage(report, config)
    check_env_keys(report, config)
    check_packages(report)
    check_database(report)
    check_disk(report, config)

    print(report.render())
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
