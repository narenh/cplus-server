#!/bin/sh
# Apply migrations, then serve. Running `alembic upgrade head` on every start is
# what makes an image upgrade a no-op for the operator: pull, restart, done.
set -eu

echo "cplus-service: applying migrations to ${CPLUS_DB_PATH}"
alembic upgrade head

echo "cplus-service: listening on ${CPLUS_HOST}:${CPLUS_PORT}"
exec python -m cplus_service
