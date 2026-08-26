"""Deploy-time configuration, read from the environment.

Almost everything this service knows is runtime config in the database, edited
through the admin UI. The Seerr URL is deliberately not: it is the **trust root**
of the whole install, not a feature setting. Seerr decides who is allowed to be
admin, so whoever controls which Seerr gets asked controls who gets in. That is
a deploy-time decision — same category as ``CPLUS_DB_PATH`` — and it belongs
somewhere only the person with access to the deployment can set it.

Prowlarr stays in the database. It holds credentials, but it has no say in
identity, so pointing it somewhere else cannot make anyone an admin.

Read fresh on every call rather than captured at import, so a test can point an
install somewhere else without reloading the module. In production the value
cannot change without a restart anyway — that is the point.
"""

from __future__ import annotations

import hashlib
import logging
import os

logger = logging.getLogger(__name__)

SEERR_URL_ENV = "CPLUS_SEERR_URL"


def seerr_url() -> str | None:
    """The configured Seerr base URL, or ``None`` when the install is unconfigured.

    Normalised the way the old admin form normalised it — trimmed, no trailing
    slash — so a value that differs only in those respects is not read as a
    different instance and does not trigger a credential flush.

    Also rejects anything that is not plausibly a URL. ``docker-compose.yml``
    leans on ``${CPLUS_SEERR_URL:?set CPLUS_SEERR_URL to your Seerr base URL}``
    to fail the deploy when the variable is missing — but that ``:?`` operator
    is a real Compose feature, not universal shell syntax, and orchestration
    tooling that doesn't implement it (some Compose-alikes treat ``:?`` as a
    plain default, the same as ``:-``) will happily hand the literal error
    message to the app as if it were the value. Without this check that string
    passes the truthiness test above and the login page renders it as though
    it were a real, configured Seerr host — the exact "unconfigured install
    looks configured" bug this whole page exists to avoid.
    """
    value = os.environ.get(SEERR_URL_ENV, "").strip().rstrip("/")
    if not value:
        return None
    if not (value.startswith("http://") or value.startswith("https://")):
        logger.error(
            "%s is set but is not a URL (%r) — treating the install as"
            " unconfigured. If this came from docker-compose.yml's"
            " ':?' default, your orchestration tool likely does not"
            " support that operator and substituted its message text"
            " instead of failing the deploy.",
            SEERR_URL_ENV,
            value,
        )
        return None
    return value


def seerr_url_fingerprint() -> str:
    """A stable, non-reversible key for the configured Seerr instance.

    Stored in the ``config`` row purely so a change can be *detected* across
    restarts — see :func:`~cplus_service.auth.identity.sync_seerr_instance`. A
    fingerprint rather than the URL itself so the database never becomes a
    second, potentially stale, answer to "which Seerr is this install using?":
    there is exactly one answer, the environment, and everything that displays
    it reads it from there.
    """
    return hashlib.sha256((seerr_url() or "").encode("utf-8")).hexdigest()


__all__ = ["SEERR_URL_ENV", "seerr_url", "seerr_url_fingerprint"]
