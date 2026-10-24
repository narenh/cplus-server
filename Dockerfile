# Two stages so build tooling never ships: the service and its dependencies
# are installed into a virtualenv here, and only that virtualenv is copied
# into the runtime image.
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
# pip itself is only needed to fill the virtualenv, so it leaves with this stage.
RUN pip install . \
    && pip uninstall -y pip


FROM python:3.12-slim

# MALLOC_ARENA_MAX: glibc otherwise keeps a memory arena per thread it has
# seen, and the threads the database driver and DNS lookups use each grow one.
# Two is plenty for a service that does almost all its work on one event loop,
# and keeps idle memory from creeping up over days.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2 \
    CPLUS_DB_PATH=/data/cplus.db \
    CPLUS_HOST=0.0.0.0 \
    CPLUS_PORT=8080 \
    CPLUS_LOG_LEVEL=info \
    TZ=UTC

WORKDIR /app

COPY --from=build /opt/venv /opt/venv

# Migrations are not part of the package; they are run by the entrypoint.
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /data

EXPOSE 8080
VOLUME ["/data"]

# urlopen raises on a non-2xx, so no status check is needed; kept to one line
# so there is no shell continuation to get wrong.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ['CPLUS_PORT']+'/health', timeout=4)" || exit 1

# Starts as root only to hand /data to PUID:PGID (default 10001), then drops to
# that user for everything else; see docker/entrypoint.sh. Running the
# container with --user instead skips that step and runs as the given user.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
