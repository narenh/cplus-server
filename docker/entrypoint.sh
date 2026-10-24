#!/bin/sh
# Apply migrations, then serve. Running `alembic upgrade head` on every start is
# what makes an image upgrade a no-op for the operator: pull, restart, done.
set -eu

# Started as root (the default): make /data belong to PUID:PGID, then run the
# rest of this script again as that user. PUID/PGID let a host folder mounted
# at /data (`-v ./data:/data`) stay owned by whoever owns it on the host, the
# same convention as most self-hosted images. The chown is skipped when /data
# already belongs to them, so it costs nothing on an ordinary restart.
#
# Started as anyone else (`--user`, or a platform that never runs containers as
# root): there is nothing to drop, and /data has to be writable by that user.
if [ "$(id -u)" = "0" ]; then
    PUID="${PUID:-10001}"
    PGID="${PGID:-10001}"
    if [ "$(stat -c %u:%g /data)" != "${PUID}:${PGID}" ]; then
        echo "cplus-service: giving /data to ${PUID}:${PGID}"
        chown -R "${PUID}:${PGID}" /data
    fi
    exec setpriv --reuid="${PUID}" --regid="${PGID}" --clear-groups -- "$0" "$@"
fi

if [ ! -w /data ]; then
    echo "cplus-service: /data isn't writable by uid $(id -u). Fix its ownership on the host, or set PUID/PGID to its owner." >&2
    exit 1
fi

echo "cplus-service: applying migrations to ${CPLUS_DB_PATH}"
alembic upgrade head

echo "cplus-service: listening on ${CPLUS_HOST}:${CPLUS_PORT}"
exec python -m cplus_service
