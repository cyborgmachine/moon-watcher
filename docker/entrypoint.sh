#!/bin/bash
set -e

echo "Starting Moon Watcher..."
echo "DB_HOST=${DB_HOST:-postgres}  DB_NAME=${DB_NAME:-moon_watcher}"

# wait for PostgreSQL (compose healthcheck should cover this, but be safe)
MAX_WAIT=30
DB_READY=false
for i in $(seq 1 $MAX_WAIT); do
    if python -c "
import psycopg2, os
psycopg2.connect(
    host=os.getenv('DB_HOST','postgres'),
    port=os.getenv('DB_PORT','5432'),
    dbname=os.getenv('DB_NAME','moon_watcher'),
    user=os.getenv('DB_USER','watcher'),
    password=os.getenv('DB_PASSWORD','')
).close()
" 2>/dev/null; then
        echo "PostgreSQL is ready"
        DB_READY=true
        break
    fi
    echo "Waiting for PostgreSQL... ($i/$MAX_WAIT)"
    sleep 2
done

if [ "$DB_READY" != "true" ]; then
    echo "ERROR: PostgreSQL did not become ready in time. Exiting."
    exit 1
fi

exec python -m moon_watcher
