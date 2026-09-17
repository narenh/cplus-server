"""Typed views over the bits of Radarr's API we actually consume.

One model so far. Radarr is configured but not yet used for anything beyond
proving the credentials work, so ``/api/v3/system/status`` is the whole
surface — see :mod:`cplus_service.radarr.client`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SystemStatus(BaseModel):
    """Response of ``/api/v3/system/status`` — enough to prove the key works."""

    model_config = ConfigDict(extra="ignore")

    version: str | None = None
    app_name: str | None = Field(default=None, alias="appName")
    instance_name: str | None = Field(default=None, alias="instanceName")
