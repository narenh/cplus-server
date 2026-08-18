# Two stages so build tooling never ships: the wheel is built once, then
# installed into a clean runtime layer.
FROM python:3.12-slim AS build

WORKDIR /build
RUN pip install --no-cache-dir hatchling

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CPLUS_DB_PATH=/data/cplus.db \
    CPLUS_HOST=0.0.0.0 \
    CPLUS_PORT=8080

WORKDIR /app

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Migrations are not part of the wheel; they are run by the entrypoint.
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Runs unprivileged. /data is a volume, so it is chowned at start rather than
# here — a named volume mounted over this path would hide a build-time chown.
RUN useradd --system --create-home --uid 10001 cplus \
    && mkdir -p /data \
    && chown cplus:cplus /data

EXPOSE 8080
VOLUME ["/data"]

# urlopen raises on a non-2xx, so no status check is needed; kept to one line
# so there is no shell continuation to get wrong.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ['CPLUS_PORT']+'/health', timeout=4)" || exit 1

USER cplus
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
