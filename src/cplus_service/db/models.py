"""SQLite schema for cplus-service.

Most of these tables are not populated until stage 2 (auth, endpoints, admin
UI); they are defined now so the migration history has a single starting point.

The Prowlarr-side identifiers (``preferred_indexer_id``, ``download_client_id``,
``indexer_id``) are Prowlarr's own integer ids.  We deliberately do not mirror
Prowlarr's indexer or download-client tables locally — they are fetched live so
an admin never edits a stale copy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

CONFIG_SINGLETON_ID = 1


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base; ``Base.metadata`` is what Alembic autogenerates from."""


class EventType(StrEnum):
    SEARCH = "search"
    GRAB = "grab"


class Config(Base):
    """Singleton configuration row.

    Enforced as a singleton by a CHECK constraint rather than by convention, so
    a second row cannot be inserted by accident.  ``preferred_indexer_id`` is
    nullable and ``None`` means the admin's "All indexers" dropdown default.
    """

    __tablename__ = "config"
    __table_args__ = (CheckConstraint("id = 1", name="ck_config_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=CONFIG_SINGLETON_ID)
    seerr_url: Mapped[str | None] = mapped_column(String(512))
    prowlarr_url: Mapped[str | None] = mapped_column(String(512))
    prowlarr_api_key: Mapped[str | None] = mapped_column(String(256))
    preferred_indexer_id: Mapped[int | None] = mapped_column(Integer)


class User(Base):
    """A Seerr user permitted to use this service.

    Identity is owned by Seerr — ``seerr_user_id`` is the join key and
    ``plex_username`` is cached purely for display in the admin UI.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seerr_user_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    plex_username: Mapped[str] = mapped_column(String(256))

    actions: Mapped[list[Action]] = relationship(
        secondary="permissions", back_populates="users", lazy="selectin"
    )


class QualityProfile(Base):
    """A named, ordered list of rules.

    ``rules`` is the JSON serialisation of
    :class:`cplus_service.quality.models.QualityProfile.rules` — an ordered list
    of rule objects, each discriminated by its ``type`` key.  Order is
    meaningful and must be preserved on read.
    """

    __tablename__ = "quality_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    actions: Mapped[list[Action]] = relationship(back_populates="quality_profile")


class Action(Base):
    """An admin-defined action, e.g. "Stream Now" or "Add 4K".

    Maps a Prowlarr download client to a quality profile.  Users are granted a
    subset of actions via :class:`Permission`.
    """

    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    download_client_id: Mapped[int] = mapped_column(Integer)
    quality_profile_id: Mapped[int] = mapped_column(
        ForeignKey("quality_profiles.id", ondelete="RESTRICT")
    )

    quality_profile: Mapped[QualityProfile] = relationship(
        back_populates="actions", lazy="selectin"
    )
    users: Mapped[list[User]] = relationship(secondary="permissions", back_populates="actions")


class Permission(Base):
    """Many-to-many grant of an action to a user.

    The composite primary key is the uniqueness guarantee — a user cannot be
    granted the same action twice.
    """

    __tablename__ = "permissions"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    action_id: Mapped[int] = mapped_column(
        ForeignKey("actions.id", ondelete="CASCADE"), primary_key=True
    )


class Grab(Base):
    """A record of a release a user sent to a download client.

    The release fields are denormalised copies, not references: Prowlarr search
    results are ephemeral, so the grab has to carry everything needed to display
    the history later.
    """

    __tablename__ = "grabs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Nullable so deleting an action does not destroy the grab history that
    # referenced it.
    action_id: Mapped[int | None] = mapped_column(ForeignKey("actions.id", ondelete="SET NULL"))
    release_title: Mapped[str] = mapped_column(String(1024))
    release_guid: Mapped[str] = mapped_column(String(1024))
    indexer_id: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class ActivityLog(Base):
    """Append-only audit trail of searches and grabs.

    ``detail`` is free-form JSON whose shape depends on ``event_type``; nothing
    queries into it, so it stays deliberately unstructured.
    """

    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[EventType] = mapped_column(String(32))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
