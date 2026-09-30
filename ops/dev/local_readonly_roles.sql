-- NEXUS read-only roles — LOCAL DEV ONLY (the iMac's Postgres).
--
-- NEVER run this on the server. Production uses ops/deploy/readonly_roles.sql,
-- whose grants name the `nexus` owner and the `nexus` database. Locally the
-- tables are owned by whoever restored them (not "nexus"), and the databases
-- are nexus_dev (tests) and nexus_prodcopy (read-only copy of production).
--
-- Run once, as the local superuser that owns those databases:
--     psql -X -v ON_ERROR_STOP=1 -d postgres -f ops/dev/local_readonly_roles.sql
-- Safe to re-run: role creation is guarded, grants are idempotent.
--
-- No credential lives in this file. The local pg_hba.conf trusts local
-- sockets, so the role logs in without one. If your local auth demands one,
-- set it interactively with \password observatory and keep it out of files.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_readonly') THEN
        CREATE ROLE nexus_readonly NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'observatory') THEN
        CREATE ROLE observatory LOGIN IN ROLE nexus_readonly;
    END IF;
END
$$;

ALTER ROLE observatory SET default_transaction_read_only = on;
ALTER ROLE observatory SET statement_timeout = '5s';
-- Production allows 5. Locally the test suite and a running demo can overlap,
-- so a little more headroom.
ALTER ROLE observatory CONNECTION LIMIT 10;

-- nexus_dev: where the tests run.
\connect nexus_dev
GRANT CONNECT ON DATABASE nexus_dev TO nexus_readonly;
GRANT USAGE ON SCHEMA public TO nexus_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO nexus_readonly;
-- Tables the local owner (the role running this script) creates later, e.g.
-- future migrations applied by the test suite.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO nexus_readonly;

-- nexus_prodcopy: read-only copy of production, for demos and audits.
\connect nexus_prodcopy
GRANT CONNECT ON DATABASE nexus_prodcopy TO nexus_readonly;
GRANT USAGE ON SCHEMA public TO nexus_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO nexus_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO nexus_readonly;
