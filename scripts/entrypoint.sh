#!/bin/sh
# Applies migrations (and optionally seeds the Super Admin) before starting the API.
set -e

echo "Applying database migrations..."
alembic upgrade head

if [ "${SEED_ON_START:-true}" = "true" ]; then
    python -m scripts.seed
fi

exec "$@"
