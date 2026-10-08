#!/bin/sh
# Runs once, on first start of an empty data volume (ADR-005, ADR-009).
#
# For each database:
#   <db>_owner    LOGIN, owns the database and every table; runs migrations.
#   <db>_runtime  LOGIN, the role the service connects as; owns nothing.
#   <db>_app      NOLOGIN group holding table privileges; <db>_runtime is a member.
# Migrations grant and revoke on <db>_app, so they never need login names or passwords.
set -eu

create_database() {
  db="$1"; owner_pw="$2"; runtime_pw="$3"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE ${db}_owner LOGIN PASSWORD '${owner_pw}';
CREATE ROLE ${db}_app NOLOGIN;
CREATE ROLE ${db}_runtime LOGIN PASSWORD '${runtime_pw}' IN ROLE ${db}_app;
CREATE DATABASE ${db} OWNER ${db}_owner;
-- Postgres grants CONNECT to PUBLIC by default, so "no grants" alone means nothing.
REVOKE CONNECT, TEMPORARY ON DATABASE ${db} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${db} TO ${db}_owner, ${db}_runtime;
SQL
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<SQL
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO ${db}_app;
-- Tables created later by the owner (migrations) are readable and writable by the app
-- group, but never deletable or truncatable. Exceptions (append-only tables) are
-- revoked in the migration that creates them.
ALTER DEFAULT PRIVILEGES FOR ROLE ${db}_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE ON TABLES TO ${db}_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ${db}_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO ${db}_app;
SQL
}

create_database agent "$AGENT_DB_OWNER_PASSWORD" "$AGENT_DB_RUNTIME_PASSWORD"
create_database refund_api "$REFUND_DB_OWNER_PASSWORD" "$REFUND_DB_RUNTIME_PASSWORD"
