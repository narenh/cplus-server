"""One user's own Home — Carousel, Top Shelf and Shelves, editable per-user.

Same coverage shape as ``test_admin_libraries.py``'s own Home section, plus
what is unique to this being per-user: seeding from the current global
default on first visit, and independence from both the global default and
every other user afterwards.
"""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import User, UserHomeSettings
from cplus_service.db.session import get_config

from .test_admin_libraries import current_config, default_library
from .test_admin_webui import signed_in


async def target_user(db: AsyncSession, *, seerr_user_id: int = 2, username: str = "alice") -> User:
    user = User(seerr_user_id=seerr_user_id, plex_username=username)
    db.add(user)
    await db.flush()
    await db.commit()
    return user


async def current_home(db: AsyncSession, user_id: int) -> UserHomeSettings | None:
    """One user's Home row as it stands right now.

    ``populate_existing`` rather than ``current_config``'s own
    ``db.expire_all()``: every write in these tests happens on a *different*
    session (the one the ASGI request itself opens), so a cached copy here
    has to be refreshed to see it — but expiring the whole session would
    also expire the ``User`` rows these tests hold onto and read ``.id``
    from after this call, which needs no refresh at all.
    """
    return await db.get(UserHomeSettings, user_id, populate_existing=True)


# --------------------------------------------------------------------------- #
# The page itself, and seeding
# --------------------------------------------------------------------------- #


async def test_visiting_an_unknown_users_home_is_404(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.get("/admin/users/999/home")
    assert response.status_code == 404


async def test_a_fresh_visit_creates_no_row_until_seen(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    user = await target_user(db)
    await signed_in(client, db)

    assert await current_home(db, user.id) is None

    response = await client.get(f"/admin/users/{user.id}/home")

    assert response.status_code == 200
    assert await current_home(db, user.id) is not None


async def test_a_users_home_seeds_from_the_current_global_default(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    config = await get_config(db)
    config.home_carousel_enabled = False
    config.home_carousel_include_on_deck = True
    await db.commit()
    user = await target_user(db)
    await signed_in(client, db)

    await client.get(f"/admin/users/{user.id}/home")

    home = await current_home(db, user.id)
    assert home is not None
    assert home.home_carousel_enabled is False
    assert home.home_carousel_include_on_deck is True
    assert [shelf["id"] for shelf in home.home_shelves] == [
        shelf["id"] for shelf in config.home_shelves
    ]


async def test_a_users_home_does_not_track_later_changes_to_the_default(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    user = await target_user(db)
    await signed_in(client, db)
    await client.get(f"/admin/users/{user.id}/home")  # seeds it

    config = await get_config(db)
    config.home_carousel_enabled = False
    await db.commit()

    home = await current_home(db, user.id)
    assert home is not None
    assert home.home_carousel_enabled is True  # unchanged by the later default edit


async def test_seeding_leaves_the_document_stamp_untouched(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Seeding is not an edit — same as CanopyPlus's own HomeSettings()
    # starting at .distantPast until a person actually changes something.
    user = await target_user(db)
    await signed_in(client, db)

    await client.get(f"/admin/users/{user.id}/home")

    home = await current_home(db, user.id)
    assert home is not None
    assert home.home_modified_at is None


async def test_editing_a_users_home_stamps_one_document_wide_timestamp(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    user = await target_user(db)
    await signed_in(client, db)

    response = await client.post(f"/admin/users/{user.id}/home/shelves")

    assert response.status_code == 200
    home = await current_home(db, user.id)
    assert home is not None
    assert home.home_modified_at is not None
    # The global default's own stamp is untouched by a per-user edit.
    assert (await get_config(db)).home_modified_at is None


# --------------------------------------------------------------------------- #
# Shelves: isolation from Config and from other users
# --------------------------------------------------------------------------- #


async def test_adding_a_shelf_for_one_user_leaves_the_default_untouched(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    user = await target_user(db)
    await signed_in(client, db)
    config_before = len((await get_config(db)).home_shelves)

    response = await client.post(f"/admin/users/{user.id}/home/shelves")

    assert response.status_code == 200
    assert 'id="home-shelves"' in response.text
    home = await current_home(db, user.id)
    assert home is not None
    assert len(home.home_shelves) == config_before + 1
    assert len((await current_config(db)).home_shelves) == config_before


async def test_two_users_shelves_are_independent(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    alice = await target_user(db, seerr_user_id=2, username="alice")
    bob = await target_user(db, seerr_user_id=3, username="bob")
    await signed_in(client, db)
    await client.get(f"/admin/users/{bob.id}/home")  # seeds bob's own copy

    await client.post(f"/admin/users/{alice.id}/home/shelves")

    alice_home = await current_home(db, alice.id)
    bob_home = await current_home(db, bob.id)
    assert alice_home is not None
    assert bob_home is not None
    assert len(alice_home.home_shelves) == len(bob_home.home_shelves) + 1


async def test_removing_the_last_shelf_for_a_user_is_rejected(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    user = await target_user(db)
    await signed_in(client, db)
    await client.get(f"/admin/users/{user.id}/home")  # seeds it
    home = await current_home(db, user.id)
    assert home is not None
    assert len(home.home_shelves) == 1
    shelf_id = home.home_shelves[0]["id"]

    response = await client.post(f"/admin/users/{user.id}/home/shelves/{shelf_id}/remove")

    assert response.status_code == 400
    home = await current_home(db, user.id)
    assert home is not None
    assert len(home.home_shelves) == 1


async def test_reordering_a_users_shelves(client: httpx.AsyncClient, db: AsyncSession) -> None:
    user = await target_user(db)
    await signed_in(client, db)
    await client.post(f"/admin/users/{user.id}/home/shelves")
    home = await current_home(db, user.id)
    assert home is not None
    first_id, second_id = (shelf["id"] for shelf in home.home_shelves)

    await client.post(
        f"/admin/users/{user.id}/home/shelves/reorder",
        data={"order": [second_id, first_id]},
    )

    home = await current_home(db, user.id)
    assert home is not None
    assert [shelf["id"] for shelf in home.home_shelves] == [second_id, first_id]


async def test_moving_a_users_shelf_up(client: httpx.AsyncClient, db: AsyncSession) -> None:
    user = await target_user(db)
    await signed_in(client, db)
    await client.post(f"/admin/users/{user.id}/home/shelves")
    home = await current_home(db, user.id)
    assert home is not None
    first_id, second_id = (shelf["id"] for shelf in home.home_shelves)

    response = await client.post(f"/admin/users/{user.id}/home/shelves/{second_id}/move-up")

    assert response.status_code == 200
    home = await current_home(db, user.id)
    assert home is not None
    assert [shelf["id"] for shelf in home.home_shelves] == [second_id, first_id]


async def test_updating_a_users_shelf_source(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await default_library(db, library_id="1", server_title="Movies (4K HDR)", name="Movies")
    user = await target_user(db)
    await signed_in(client, db)
    await client.get(f"/admin/users/{user.id}/home")  # seeds it
    home = await current_home(db, user.id)
    assert home is not None
    shelf_id = home.home_shelves[0]["id"]

    response = await client.post(
        f"/admin/users/{user.id}/home/shelves/{shelf_id}",
        data={"source": "lib:1:newest", "style": "card"},
    )

    assert response.status_code == 200
    home = await current_home(db, user.id)
    assert home is not None
    shelf = home.home_shelves[0]
    assert shelf["path"] == "/library/sections/1/newest"
    assert shelf["title"] == "Recently Released Movies"
    # The global default's own shelf is untouched.
    assert (await current_config(db)).home_shelves[0]["path"] == "/library/onDeck"


async def test_updating_an_unknown_shelf_for_a_user_is_404(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    user = await target_user(db)
    await signed_in(client, db)

    response = await client.post(
        f"/admin/users/{user.id}/home/shelves/nope", data={"source": "ondeck"}
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Carousel and Top Shelf
# --------------------------------------------------------------------------- #


async def test_setting_a_users_carousel_source(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await default_library(db, library_id="1", server_title="Movies")
    user = await target_user(db)
    await signed_in(client, db)

    response = await client.post(
        f"/admin/users/{user.id}/home/carousel", data={"source": "lib:1:all"}
    )

    assert response.status_code == 200
    assert 'id="carousel-section"' in response.text
    home = await current_home(db, user.id)
    assert home is not None
    assert home.home_carousel is not None
    assert home.home_carousel["path"] == "/library/sections/1/all"
    # The global default's own carousel is untouched.
    config = await get_config(db)
    assert config.home_carousel["path"] == "/library/onDeck"


async def test_toggling_a_users_carousel_enabled(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    user = await target_user(db)
    await signed_in(client, db)

    response = await client.post(f"/admin/users/{user.id}/home/carousel-enabled", data={})

    assert response.status_code == 200
    home = await current_home(db, user.id)
    assert home is not None
    assert home.home_carousel_enabled is False
    # The global default's own switch is untouched.
    assert (await get_config(db)).home_carousel_enabled is True


async def test_setting_a_users_top_shelf_source(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await default_library(db, library_id="1", server_title="Movies")
    user = await target_user(db)
    await signed_in(client, db)

    response = await client.post(
        f"/admin/users/{user.id}/home/top-shelf", data={"source": "lib:1:newest"}
    )

    assert response.status_code == 200
    assert 'id="top-shelf-section"' in response.text
    home = await current_home(db, user.id)
    assert home is not None
    assert home.home_top_shelf is not None
    assert home.home_top_shelf["path"] == "/library/sections/1/newest"
    assert (await get_config(db)).home_top_shelf["path"] == "/library/onDeck"
