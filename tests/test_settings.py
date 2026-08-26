"""Unit tests for the deploy-time settings in :mod:`cplus_service.settings`."""

from __future__ import annotations

import pytest

from cplus_service.settings import SEERR_URL_ENV, seerr_url


def test_unset_is_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SEERR_URL_ENV, raising=False)
    assert seerr_url() is None


def test_blank_is_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SEERR_URL_ENV, "   ")
    assert seerr_url() is None


def test_a_real_url_is_trimmed_and_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SEERR_URL_ENV, "  http://seerr.test:5055/  ")
    assert seerr_url() == "http://seerr.test:5055"


def test_https_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SEERR_URL_ENV, "https://seerr.test")
    assert seerr_url() == "https://seerr.test"


def test_a_docker_compose_required_var_message_is_not_a_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards the exact failure mode ``docker-compose.yml`` can produce.

    ``CPLUS_SEERR_URL: ${CPLUS_SEERR_URL:?set CPLUS_SEERR_URL to your Seerr base URL}``
    is meant to fail the deploy when the variable is missing. Orchestration
    tooling that doesn't implement Compose's ``:?`` operator can instead hand
    the app that literal message as the value — which must not be read as a
    configured Seerr host.
    """
    monkeypatch.setenv(SEERR_URL_ENV, "set CPLUS_SEERR_URL to your Seerr base URL")
    assert seerr_url() is None


def test_a_non_url_value_is_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SEERR_URL_ENV, "seerr.test:5055")
    assert seerr_url() is None
