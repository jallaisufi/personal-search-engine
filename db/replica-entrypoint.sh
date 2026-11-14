#!/usr/bin/env bash
# Starts a streaming read replica.
# First start: copy the primary's data with pg_basebackup. `-R` writes standby.signal and
# the connection settings, so Postgres comes up in recovery and follows the primary's WAL.
# Later starts: the data directory already exists, so it simply resumes streaming.
set -euo pipefail

: "${PGDATA:=/var/lib/postgresql/data}"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "replica: waiting for the primary at ${PRIMARY_HOST}..."
  until pg_isready -h "$PRIMARY_HOST" -U "$POSTGRES_USER" -q; do sleep 1; done

  echo "replica: copying the primary's data (base backup)..."
  mkdir -p "$PGDATA"
  chown -R postgres:postgres "$PGDATA"
  chmod 700 "$PGDATA"
  gosu postgres pg_basebackup \
    -d "host=${PRIMARY_HOST} user=${POSTGRES_USER} password=${POSTGRES_PASSWORD}" \
    -D "$PGDATA" -Fp -Xs -R
  echo "replica: base backup done"
fi

# hot_standby_feedback: tell the primary which rows running searches still need, so
# cleanup on the primary doesn't cancel queries here.
# max_connections must be at least the primary's for a hot standby.
exec docker-entrypoint.sh postgres \
  -c hot_standby_feedback=on \
  -c max_connections="${PG_MAX_CONNECTIONS:-200}"
