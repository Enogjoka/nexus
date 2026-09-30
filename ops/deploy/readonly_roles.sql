-- NEXUS read-only database roles (plan Appendix D; Task O1).
--
-- Run ONCE on the server, as the Postgres superuser, against the production
-- database:
--     su - postgres -c "psql -d nexus -f /home/nexus/nexus/ops/deploy/readonly_roles.sql"
--
-- This file creates the roles WITHOUT credentials, on purpose. Set each
-- login role's secret interactively in psql, never in a file, never in git,
-- never in chat:
--     \password observatory
--     \password architect_agent
--
-- The real protection is the privilege set: SELECT only. The read-only
-- session default is a second belt, which a session could override.
--
-- Acceptance after running: connected as observatory,
--     INSERT INTO kernel_events (breaker, action, reason) VALUES ('x', 'x', 'x');
-- must fail with "permission denied for table kernel_events".

-- Group role: owns the SELECT grants, cannot log in.
CREATE ROLE nexus_readonly NOLOGIN;
GRANT CONNECT ON DATABASE nexus TO nexus_readonly;
GRANT USAGE ON SCHEMA public TO nexus_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO nexus_readonly;
-- Tables created later by the trading role (future migrations) are covered too.
ALTER DEFAULT PRIVILEGES FOR ROLE nexus IN SCHEMA public GRANT SELECT ON TABLES TO nexus_readonly;

-- The Observatory projector/API (Appendix B): short queries, few connections.
CREATE ROLE observatory LOGIN IN ROLE nexus_readonly;
ALTER ROLE observatory SET default_transaction_read_only = on;
ALTER ROLE observatory SET statement_timeout = '5s';
ALTER ROLE observatory CONNECTION LIMIT 5;

-- The Head Architect agent (Appendix C): longer audit queries, fewer connections.
CREATE ROLE architect_agent LOGIN IN ROLE nexus_readonly;
ALTER ROLE architect_agent SET default_transaction_read_only = on;
ALTER ROLE architect_agent SET statement_timeout = '30s';
ALTER ROLE architect_agent CONNECTION LIMIT 3;
