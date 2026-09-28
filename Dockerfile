# Two stages so build tooling never ships: the wheel is built once, then
# installed into a clean runtime layer.
FROM python:3.12-slim AS build

WORKDIR /build
RUN pip install --no-cache-dir hatchling

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .


# The audiobook aligner's one compiled piece: ctc-forced-aligner's C++ Viterbi,
# built here so no compiler ships. It is plain pybind11 — no torch at build
# time. Only this small wheel goes into the image; torch, the other Python
# packages and the model are downloaded into the aligner's volume when an admin
# turns read-along on (see cplus_align.install). Its own stage so the compiler
# layer is cached apart from every change to this service's code.
#
# It is installed from GitHub at a pinned commit, not from PyPI: the PyPI
# package named ctc-forced-aligner is an unrelated project.
FROM python:3.12-slim AS aligner-ext

ARG CTC_FORCED_ALIGNER_COMMIT=64293cc6d711e57666c4a8b098e9fd93b381fd88
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /aligner-wheels \
        "git+https://github.com/MahmoudAshraf97/ctc-forced-aligner.git@${CTC_FORCED_ALIGNER_COMMIT}" \
    && echo "${CTC_FORCED_ALIGNER_COMMIT}" > /aligner-wheels/SOURCE


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CPLUS_DB_PATH=/data/cplus.db \
    CPLUS_HOST=0.0.0.0 \
    CPLUS_PORT=8080 \
    CPLUS_LOG_LEVEL=info \
    TZ=UTC

WORKDIR /app

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
COPY --from=aligner-ext /aligner-wheels /opt/cplus-align/wheels

# Migrations are not part of the wheel; they are run by the entrypoint.
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Runs unprivileged. /data is a volume, so it is chowned at start rather than
# here — a named volume mounted over this path would hide a build-time chown.
# /align is the audiobook aligner's shared volume; a fresh named volume takes
# its ownership from this directory, which is what lets both containers write it.
RUN useradd --system --create-home --uid 10001 cplus \
    && mkdir -p /data /align \
    && chown cplus:cplus /data /align

EXPOSE 8080
VOLUME ["/data"]

# urlopen raises on a non-2xx, so no status check is needed; kept to one line
# so there is no shell continuation to get wrong.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ['CPLUS_PORT']+'/health', timeout=4)" || exit 1

USER cplus
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
