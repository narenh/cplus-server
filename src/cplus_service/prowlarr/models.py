"""Typed views over the bits of Prowlarr's API we actually consume."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Indexer(BaseModel):
    """A Prowlarr indexer, as offered in the admin's preferred-indexer dropdown."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str = ""
    enable: bool = True
    protocol: str | None = None
    privacy: str | None = None


class DownloadClient(BaseModel):
    """A Prowlarr download client, as bound to an action."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str = ""
    enable: bool = True
    protocol: str | None = None
    priority: int | None = None


class SystemStatus(BaseModel):
    """Response of ``/api/v1/system/status`` — enough to prove the key works."""

    model_config = ConfigDict(extra="ignore")

    version: str | None = None
    app_name: str | None = Field(default=None, alias="appName")
    instance_name: str | None = Field(default=None, alias="instanceName")


class GrabResult(BaseModel):
    """Outcome of a grab.

    Prowlarr returns ``201`` with an echo of the release on success; we keep the
    raw body so stage 2 can log it verbatim into ``activity_log.detail``.
    """

    model_config = ConfigDict(extra="ignore")

    guid: str
    indexer_id: int | None = None
    download_client_id: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
