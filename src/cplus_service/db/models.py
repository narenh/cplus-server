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
    Boolean,
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


class ApnsEnvironment(StrEnum):
    """Which APNs host a device token is valid against.

    Not a preference: a token minted by a development build is meaningless to
    the production host and vice versa, so it is a property of the token and is
    stored per device rather than set once for the whole install.
    """

    SANDBOX = "sandbox"
    PRODUCTION = "production"


class Config(Base):
    """Singleton configuration row.

    Enforced as a singleton by a CHECK constraint rather than by convention, so
    a second row cannot be inserted by accident.  ``preferred_indexer_id`` is
    nullable and ``None`` means the admin's "All indexers" dropdown default.
    """

    __tablename__ = "config"
    __table_args__ = (CheckConstraint("id = 1", name="ck_config_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=CONFIG_SINGLETON_ID)

    #: SHA-256 of the ``CPLUS_SEERR_URL`` this install was last serving under.
    #:
    #: Not the URL — the environment is the only answer to "which Seerr?", and a
    #: second copy here could disagree with it. This is a change *detector* and
    #: nothing reads it for display: on startup a mismatch means the deployment
    #: was repointed, and every credential resolved against the old instance is
    #: flushed. See :func:`~cplus_service.auth.identity.sync_seerr_instance`.
    seerr_url_fingerprint: Mapped[str | None] = mapped_column(String(64))

    prowlarr_url: Mapped[str | None] = mapped_column(String(512))
    prowlarr_api_key: Mapped[str | None] = mapped_column(String(256))
    preferred_indexer_id: Mapped[int | None] = mapped_column(Integer)

    #: TMDB's v4 read-access bearer token. Stored the same way as
    #: ``prowlarr_api_key`` — plaintext in this row, never rendered back into a
    #: page. Unlike the Prowlarr key it is also exposed verbatim over the API
    #: (``GET /manager/tmdb-token``, ADMIN-bit gated) for testing purposes; see
    #: the docstring there for why that is an accepted, deliberate exception.
    tmdb_bearer_token: Mapped[str | None] = mapped_column(String(1024))

    #: Stable per-install identity for the plex.tv PIN flow.  Generated on
    #: first sign-in.  It must not change between sign-ins, or every login
    #: registers a fresh device on the admin's Plex account.
    plex_client_identifier: Mapped[str | None] = mapped_column(String(64))

    #: The master switch for push notifications, and the only setting on the
    #: Notifications tab that is **off by default**.
    #:
    #: Off by default because turning it on routes notification text through a
    #: third party — a relay this install's admin does not run — in plaintext.
    #: That is a decision an admin has to make deliberately, so nothing about
    #: notifications happens until they make it: no device registers, no
    #: capability is advertised, no push is attempted.  Every other setting on
    #: that page is inert while this is false, which is why the page hides them.
    notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )

    #: The relay identity this install was issued when notifications were
    #: switched on.  Shown on the Notifications tab so a support conversation
    #: has something to name; not a secret and not used for anything else.
    notification_relay_instance_id: Mapped[str | None] = mapped_column(String(64))

    #: The relay API key issued alongside it.  Never rendered back into a page
    #: and never exposed over the API.
    #:
    #: **No admin ever types this.**  It is obtained automatically by
    #: :func:`cplus_service.notify.relay.enrol` when notifications are switched
    #: on, which is why there is no form field for it.  It is a rate-limit
    #: identity and an abuse handle, not an access-control boundary over
    #: devices — isolation between installs comes from token custody, so a
    #: credential the admin had to manage would have been protecting nothing
    #: they chose.  See :mod:`cplus_service.notify.relay`.
    #:
    #: Not recoverable: the relay writes nothing down, so a lost key is
    #: replaced by enrolling again rather than looked up.
    notification_relay_api_key: Mapped[str | None] = mapped_column(String(256))


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
    """A named profile: filter rules, ordered choices, and tie-breakers.

    ``rules`` is the JSON serialisation of
    :class:`cplus_service.quality.models.QualityProfile.rules` — an ordered list
    of rule objects, each discriminated by its ``type`` key.  Order is
    meaningful and must be preserved on read.

    ``choices`` is the same for
    :class:`cplus_service.quality.models.Choice`: an ordered list of "I'd
    rather have this kind of release" rungs, applied ahead of the preference
    rules.  It is a separate column rather than more entries in ``rules``
    because it is a different shape of thing, and because leaving ``rules``
    untouched means every profile stored before choices existed still loads and
    still behaves identically — an empty choice list is "one pool", which is
    what those profiles always were.
    """

    __tablename__ = "quality_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    choices: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, server_default="[]"
    )

    actions: Mapped[list[Action]] = relationship(back_populates="quality_profile")


class Action(Base):
    """An admin-defined action, e.g. "Stream Now" or "Add 4K".

    Maps a Prowlarr download client to a quality profile.  Users are granted a
    subset of actions via :class:`Permission`.

    The built-in Request action is the one exception: it is seeded with
    ``is_system=True`` and carries neither a download client nor a quality
    profile, because it never touches Prowlarr.  A system action cannot be
    edited or deleted, which is what makes its name a stable identifier — the
    tvOS client routes on ``name == "Request"``.  The CHECK constraint keeps
    the nullable columns from being abused: only a system action may omit them.

    ``name`` and ``display_title`` are deliberately two different things.  The
    name is the admin's own label — unique, used in the admin UI, in grab
    history and in notification text, and for the system action it is part of
    the client contract.  The display title is the copy the client prints on
    the button, and it answers a different question: an admin may name an
    action "Add to library in HD" for their own bookkeeping while the person
    holding the remote should just see "Play Now".  It is optional; when it is
    unset the name is the button copy, which is why every existing install
    keeps behaving exactly as before.
    """

    __tablename__ = "actions"
    __table_args__ = (
        CheckConstraint(
            "is_system = 1 OR (download_client_id IS NOT NULL"
            " AND quality_profile_id IS NOT NULL)",
            name="ck_action_targets_required_unless_system",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)

    #: Optional button copy for the client.  Not unique — two actions may
    #: reasonably print the same words — and never used to identify anything.
    #: ``None`` means "use the name", which is what every action created before
    #: this column existed does.
    display_title: Mapped[str | None] = mapped_column(String(128))

    download_client_id: Mapped[int | None] = mapped_column(Integer)
    quality_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("quality_profiles.id", ondelete="RESTRICT")
    )
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    quality_profile: Mapped[QualityProfile | None] = relationship(
        back_populates="actions", lazy="selectin"
    )
    users: Mapped[list[User]] = relationship(secondary="permissions", back_populates="actions")

    @property
    def button_title(self) -> str:
        """The copy a client should print on this action's button.

        The one place the fallback lives, so no caller has to remember it.
        """
        return self.display_title or self.name


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


class AdminSession(Base):
    """A browser session for the admin webui.

    Only the webui uses sessions.  tvOS has no session concept at all — it
    presents its Plex token on every request (see
    :mod:`cplus_service.auth.plex_cache`).

    The cookie holds an opaque random token rather than signed claims, so there
    is no signing secret to manage and revocation is a row delete.  Sessions are
    persisted rather than held in memory so a restart does not log the admin out
    mid-configuration.
    """

    __tablename__ = "admin_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PlexTokenSession(Base):
    """A validated Plex token, mapped to the local user it belongs to.

    This is what lets ``/titles/{imdb_id}/actions``, ``/search`` and ``/grab``
    authenticate without an outbound call to Plex or Seerr.  ``GET /register``
    writes it after validating for real; the fast paths only ever read it.

    Rows store a SHA-256 **fingerprint**, never the token itself, so the table
    cannot hand anyone a working Plex credential even if the database file
    leaks.  There is deliberately no expiry: an entry stays valid until that
    user's next ``/register`` call overwrites it, or the user is deleted, which
    cascades.
    """

    __tablename__ = "plex_token_sessions"

    token_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class NotificationPreference(Base):
    """One admin-facing opt-out, keyed by notification type.

    **A missing row means enabled.**  That is what makes "both enabled by
    default" true without seeding anything, and it is also what lets a later
    release add a third type that is live immediately for existing installs —
    there is no backfill to forget and no migration that has to enumerate the
    types.  Rows appear only once an admin actually moves a switch.

    ``notification_type`` is the string value of
    :class:`cplus_service.notify.types.NotificationType` rather than a
    constrained column, so an unrecognised row (a type from a newer version, or
    one since removed) is inert data rather than a load failure.
    """

    __tablename__ = "notification_preferences"

    notification_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")


class ApnsDevice(Base):
    """One iOS/tvOS device registered to receive push notifications.

    Keyed by the device token itself: Apple hands the same token back to every
    launch of the same app on the same device, so re-registering is an upsert
    of ``last_seen_at`` rather than a second row.  A token can migrate between
    users (someone signs out and a different admin signs in on the same
    device), which is why ``user_id`` is an ordinary column and not part of the
    key.

    Rows are removed on their own when Apple says so — a 410 from APNs means
    the app was uninstalled and the token is dead; see
    :mod:`cplus_service.notify.apns`.
    """

    __tablename__ = "apns_devices"

    device_token: Mapped[str] = mapped_column(String(200), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    environment: Mapped[ApnsEnvironment] = mapped_column(
        String(16), default=ApnsEnvironment.PRODUCTION, server_default="production"
    )
    #: Free-form label from the registering client ("Naren's Apple TV"), shown
    #: in the admin UI so a stale device can be told apart from a live one.
    device_name: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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
