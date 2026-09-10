"""The Libraries & Home tab.

Real ASGI app, real database, real templates — the Plex Media Server is the
only thing mocked, via respx. Default Libraries and Home shelves each get
their own add/rename-or-edit/remove/reorder coverage; the carousel gets its
two on/off switches and its source picker.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from cplus_service.db.models import Config
from cplus_service.db.session import get_config

from .test_admin_webui import signed_in

PLEX_SERVER_URL = "http://plex.local:32400"


def sections_payload(*directory: dict) -> dict:
    return {"MediaContainer": {"Directory": list(directory)}}


def movies_section(key: str = "1", title: str = "Movies (4K HDR)") -> dict:
    return {"key": key, "title": title, "type": "movie", "hidden": 0}


def shows_section(key: str = "2", title: str = "TV Shows") -> dict:
    return {"key": key, "title": title, "type": "show", "hidden": 0}


@pytest.fixture
def library_sections():
    """Mock the Plex server's ``/library/sections`` for the lifetime of a test."""
    with respx.mock:
        route = respx.get(f"{PLEX_SERVER_URL}/library/sections").mock(
            return_value=httpx.Response(
                200, json=sections_payload(movies_section(), shows_section())
            )
        )
        yield route


@pytest.fixture
async def connected(db: AsyncSession) -> Config:
    """A Config row already pointed at a (mocked) Plex Media Server."""
    config = await get_config(db)
    config.plex_admin_token = "server-token"
    config.plex_server_base_url = PLEX_SERVER_URL
    config.plex_server_client_identifier = "abc123"
    config.plex_server_name = "Naren's Server"
    await db.commit()
    return config


async def default_library(
    db: AsyncSession,
    *,
    library_id: str = "1",
    server_title: str = "Movies (4K HDR)",
    name: str | None = None,
    library_type: str = "movie",
) -> Config:
    config = await get_config(db)
    config.default_libraries = [
        *config.default_libraries,
        {
            "id": library_id,
            "serverTitle": server_title,
            "type": library_type,
            "hidden": False,
            "name": name or server_title,
        },
    ]
    await db.commit()
    return config


async def current_config(db: AsyncSession) -> Config:
    """The Config row as it stands right now.

    ``db`` is a single session shared for the whole test, and every write in
    these tests actually happens on a *different* session (the one the ASGI
    request itself opens) — the same split every route in this app has. With
    ``expire_on_commit=False`` this session's own identity map does not
    automatically notice that, so anything read here after an HTTP call has to
    be expired first or it can hand back what was true before the call ran.
    """
    db.expire_all()
    return await get_config(db)


# --------------------------------------------------------------------------- #
# The page itself
# --------------------------------------------------------------------------- #


async def test_the_page_reports_not_connected_without_a_plex_server(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.get("/admin/libraries")

    assert response.status_code == 200
    assert "Not connected" in response.text


async def test_the_page_lists_available_libraries_once_connected(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, library_sections
) -> None:
    await signed_in(client, db)
    response = await client.get("/admin/libraries")

    assert response.status_code == 200
    assert '<option value="1">' in response.text
    assert '<option value="2">' in response.text
    assert "Naren&#39;s Server" in response.text or "Naren's Server" in response.text


async def test_a_configured_library_is_not_offered_again_in_the_dropdown(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, library_sections
) -> None:
    await default_library(db, library_id="1", server_title="Movies (4K HDR)")
    await signed_in(client, db)

    response = await client.get("/admin/libraries")

    # Configured already: shown as a row, offered nowhere in the "add" dropdown.
    assert 'value="Movies (4K HDR)"' in response.text
    assert '<option value="1">' not in response.text
    # The other library is still on offer.
    assert '<option value="2">' in response.text


# --------------------------------------------------------------------------- #
# Default Libraries: add / rename / remove / reorder
# --------------------------------------------------------------------------- #


async def test_adding_a_library_stores_it_in_the_canopyplus_shape(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, library_sections
) -> None:
    await signed_in(client, db)

    response = await client.post("/admin/libraries", data={"library_id": "1"})

    # No redirect: the response is the updated card itself, swapped in place.
    assert response.status_code == 200
    assert 'id="default-libraries"' in response.text
    assert "Movies (4K HDR)" in response.text
    config = await current_config(db)
    assert config.default_libraries == [
        {
            "id": "1",
            "serverTitle": "Movies (4K HDR)",
            "type": "movie",
            "hidden": False,
            "name": "Movies (4K HDR)",
        }
    ]


async def test_adding_the_same_library_twice_is_rejected(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Caught before ever asking Plex again — the library is already known.
    await default_library(db, library_id="1")
    await signed_in(client, db)

    response = await client.post("/admin/libraries", data={"library_id": "1"})
    assert response.status_code == 409


async def test_an_unknown_library_id_is_rejected(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config, library_sections
) -> None:
    await signed_in(client, db)

    response = await client.post("/admin/libraries", data={"library_id": "does-not-exist"})
    assert response.status_code == 400


async def test_there_is_no_cap_on_how_many_libraries_can_be_added(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config
) -> None:
    # The app enforces its own client-side display limit (SwiftUI's tab bar
    # caps out); the admin's own ordered, named list is not bound by that.
    with respx.mock:
        respx.get(f"{PLEX_SERVER_URL}/library/sections").mock(
            return_value=httpx.Response(
                200,
                json=sections_payload(
                    *(movies_section(key=str(i), title=f"Library {i}") for i in range(8))
                ),
            )
        )
        for i in range(7):
            await default_library(db, library_id=str(i), server_title=f"Library {i}")
        await signed_in(client, db)

        response = await client.post("/admin/libraries", data={"library_id": "7"})

    assert response.status_code == 200
    config = await current_config(db)
    assert len(config.default_libraries) == 8


async def test_music_and_photo_libraries_are_not_offered(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config
) -> None:
    with respx.mock:
        respx.get(f"{PLEX_SERVER_URL}/library/sections").mock(
            return_value=httpx.Response(
                200,
                json=sections_payload(
                    movies_section(),
                    {"key": "3", "title": "Music", "type": "artist", "hidden": 0},
                    {"key": "4", "title": "Photos", "type": "photo", "hidden": 0},
                ),
            )
        )
        await signed_in(client, db)
        response = await client.get("/admin/libraries")

    assert response.status_code == 200
    assert '<option value="1">' in response.text
    assert '<option value="3">' not in response.text
    assert '<option value="4">' not in response.text
    assert "Music" not in response.text
    assert "Photos" not in response.text


async def test_a_music_library_id_cannot_be_added_directly(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config
) -> None:
    # Not just hidden from the dropdown — the server itself never learns
    # about it, so posting the id by hand doesn't work either.
    with respx.mock:
        respx.get(f"{PLEX_SERVER_URL}/library/sections").mock(
            return_value=httpx.Response(
                200,
                json=sections_payload(
                    {"key": "3", "title": "Music", "type": "artist", "hidden": 0}
                ),
            )
        )
        await signed_in(client, db)
        response = await client.post("/admin/libraries", data={"library_id": "3"})

    assert response.status_code == 400


async def test_renaming_a_library_leaves_its_server_title_untouched(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await default_library(db, library_id="1", server_title="Movies (4K HDR)")
    await signed_in(client, db)

    response = await client.post("/admin/libraries/1/rename", data={"name": "Movies"})

    assert response.status_code == 200
    assert 'value="Movies"' in response.text
    assert "Plex: Movies (4K HDR)" in response.text
    config = await current_config(db)
    assert config.default_libraries[0]["name"] == "Movies"
    assert config.default_libraries[0]["serverTitle"] == "Movies (4K HDR)"


async def test_renaming_a_library_blank_resets_it_to_the_server_title(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await default_library(db, library_id="1", server_title="Movies (4K HDR)", name="Movies")
    await signed_in(client, db)

    await client.post("/admin/libraries/1/rename", data={"name": "   "})

    config = await current_config(db)
    assert config.default_libraries[0]["name"] == "Movies (4K HDR)"


async def test_renaming_an_unknown_library_is_404(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.post("/admin/libraries/nope/rename", data={"name": "x"})
    assert response.status_code == 404


async def test_removing_a_library(client: httpx.AsyncClient, db: AsyncSession) -> None:
    await default_library(db, library_id="1")
    await default_library(db, library_id="2", server_title="TV")
    await signed_in(client, db)

    response = await client.post("/admin/libraries/1/remove")

    assert response.status_code == 200
    assert 'data-reorder-id="1"' not in response.text
    assert 'data-reorder-id="2"' in response.text
    config = await current_config(db)
    assert [library["id"] for library in config.default_libraries] == ["2"]


async def test_reordering_libraries(client: httpx.AsyncClient, db: AsyncSession) -> None:
    await default_library(db, library_id="1", server_title="Movies")
    await default_library(db, library_id="2", server_title="TV")
    await default_library(db, library_id="3", server_title="Music")
    await signed_in(client, db)

    response = await client.post(
        "/admin/libraries/reorder", data={"order": ["3", "1", "2"]}
    )

    assert response.status_code == 200
    config = await current_config(db)
    assert [library["id"] for library in config.default_libraries] == ["3", "1", "2"]


async def test_reordering_keeps_libraries_the_form_left_out(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await default_library(db, library_id="1", server_title="Movies")
    await default_library(db, library_id="2", server_title="TV")
    await signed_in(client, db)

    await client.post("/admin/libraries/reorder", data={"order": ["2"]})

    config = await current_config(db)
    assert [library["id"] for library in config.default_libraries] == ["2", "1"]


# --------------------------------------------------------------------------- #
# Home shelves
# --------------------------------------------------------------------------- #


async def test_a_fresh_config_starts_with_the_continue_watching_shelf(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Seeded by ensure_default_home_shelf at app startup.
    config = await get_config(db)
    assert len(config.home_shelves) == 1
    assert config.home_shelves[0]["title"] == "Continue Watching"


async def test_adding_a_shelf(client: httpx.AsyncClient, db: AsyncSession) -> None:
    await signed_in(client, db)
    before = len((await get_config(db)).home_shelves)

    response = await client.post("/admin/libraries/home/shelves")

    # No redirect: the response is the updated shelf list, swapped in place.
    assert response.status_code == 200
    assert 'id="home-shelves"' in response.text
    config = await current_config(db)
    assert len(config.home_shelves) == before + 1


async def test_removing_the_last_shelf_is_rejected(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    config = await get_config(db)
    assert len(config.home_shelves) == 1
    shelf_id = config.home_shelves[0]["id"]

    response = await client.post(f"/admin/libraries/home/shelves/{shelf_id}/remove")

    assert response.status_code == 400
    assert len((await current_config(db)).home_shelves) == 1


async def test_removing_a_shelf_when_more_than_one_remains(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    await client.post("/admin/libraries/home/shelves")
    config = await current_config(db)
    first_id = config.home_shelves[0]["id"]

    response = await client.post(f"/admin/libraries/home/shelves/{first_id}/remove")

    assert response.status_code == 200
    remaining = (await current_config(db)).home_shelves
    assert len(remaining) == 1
    assert all(shelf["id"] != first_id for shelf in remaining)


async def test_reordering_shelves(client: httpx.AsyncClient, db: AsyncSession) -> None:
    await signed_in(client, db)
    await client.post("/admin/libraries/home/shelves")
    config = await current_config(db)
    first_id, second_id = (shelf["id"] for shelf in config.home_shelves)

    await client.post(
        "/admin/libraries/home/shelves/reorder",
        data={"order": [second_id, first_id]},
    )

    reordered = (await current_config(db)).home_shelves
    assert [shelf["id"] for shelf in reordered] == [second_id, first_id]


async def test_moving_a_shelf_up(client: httpx.AsyncClient, db: AsyncSession) -> None:
    # Alongside dragging (test_reordering_shelves above), not instead of it:
    # a plain form POST that needs no native HTML5 drag-and-drop support.
    await signed_in(client, db)
    await client.post("/admin/libraries/home/shelves")
    await client.post("/admin/libraries/home/shelves")
    config = await current_config(db)
    first_id, second_id, third_id = (shelf["id"] for shelf in config.home_shelves)

    response = await client.post(f"/admin/libraries/home/shelves/{third_id}/move-up")

    assert response.status_code == 200
    reordered = (await current_config(db)).home_shelves
    assert [shelf["id"] for shelf in reordered] == [first_id, third_id, second_id]


async def test_moving_a_shelf_down(client: httpx.AsyncClient, db: AsyncSession) -> None:
    await signed_in(client, db)
    await client.post("/admin/libraries/home/shelves")
    config = await current_config(db)
    first_id, second_id = (shelf["id"] for shelf in config.home_shelves)

    response = await client.post(f"/admin/libraries/home/shelves/{first_id}/move-down")

    assert response.status_code == 200
    reordered = (await current_config(db)).home_shelves
    assert [shelf["id"] for shelf in reordered] == [second_id, first_id]


async def test_moving_the_first_shelf_up_is_a_no_op(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Unreachable through the page — the button is disabled — but a stale
    # click should do nothing rather than raise or shuffle anything.
    await signed_in(client, db)
    await client.post("/admin/libraries/home/shelves")
    config = await current_config(db)
    first_id, second_id = (shelf["id"] for shelf in config.home_shelves)

    response = await client.post(f"/admin/libraries/home/shelves/{first_id}/move-up")

    assert response.status_code == 200
    reordered = (await current_config(db)).home_shelves
    assert [shelf["id"] for shelf in reordered] == [first_id, second_id]


async def test_moving_the_last_shelf_down_is_a_no_op(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    await client.post("/admin/libraries/home/shelves")
    config = await current_config(db)
    first_id, second_id = (shelf["id"] for shelf in config.home_shelves)

    response = await client.post(f"/admin/libraries/home/shelves/{second_id}/move-down")

    assert response.status_code == 200
    reordered = (await current_config(db)).home_shelves
    assert [shelf["id"] for shelf in reordered] == [first_id, second_id]


async def test_moving_an_unknown_shelf_is_a_no_op(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)

    response = await client.post("/admin/libraries/home/shelves/nope/move-up")

    assert response.status_code == 200


async def test_changing_a_shelfs_source_resets_title_style_and_title_only(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await default_library(db, library_id="1", server_title="Movies (4K HDR)", name="Movies")
    await signed_in(client, db)
    config = await current_config(db)
    shelf_id = config.home_shelves[0]["id"]

    response = await client.post(
        f"/admin/libraries/home/shelves/{shelf_id}",
        data={"source": "lib:1:newest", "title": "ignored", "style": "card"},
    )

    assert response.status_code == 200
    shelf = (await current_config(db)).home_shelves[0]
    assert shelf["path"] == "/library/sections/1/newest"
    assert shelf["title"] == "Recently Released Movies"
    assert shelf["style"] == "poster"  # movie library default, not the posted "card"
    assert shelf["titleOnly"] is True


async def test_editing_a_shelf_without_changing_its_source(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    config = await current_config(db)
    shelf_id = config.home_shelves[0]["id"]
    assert shelf_id  # ondeck shelf

    response = await client.post(
        f"/admin/libraries/home/shelves/{shelf_id}",
        data={"source": "ondeck", "title": "My Shelf", "style": "poster", "title_only": "on"},
    )

    assert response.status_code == 200
    shelf = (await current_config(db)).home_shelves[0]
    assert shelf["title"] == "My Shelf"
    assert shelf["style"] == "poster"
    assert shelf["titleOnly"] is True
    # The source itself (path/discoverHubKey/description) is untouched.
    assert shelf["path"] == "/library/onDeck"


async def test_updating_an_unknown_shelf_is_404(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.post(
        "/admin/libraries/home/shelves/nope", data={"source": "ondeck"}
    )
    assert response.status_code == 404


async def test_an_unresolvable_source_is_rejected(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    config = await current_config(db)
    shelf_id = config.home_shelves[0]["id"]

    response = await client.post(
        f"/admin/libraries/home/shelves/{shelf_id}", data={"source": "lib:999:all"}
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# Home shelves: "Collection Items…"
# --------------------------------------------------------------------------- #


def collections_route(library_id: str = "1", *collections: dict) -> None:
    """Mock ``GET /library/sections/{library_id}/collections`` for the lifetime
    of an already-open ``respx.mock`` block."""
    respx.get(f"{PLEX_SERVER_URL}/library/sections/{library_id}/collections").mock(
        return_value=httpx.Response(
            200, json={"MediaContainer": {"Metadata": list(collections)}}
        )
    )


async def test_choosing_collection_items_auto_applies_the_librarys_first_collection(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config
) -> None:
    # Picking "Collection Items…" itself is a placeholder, not a real source
    # (see COLLECTIONS_PREFIX) — there is never a moment where it shows a
    # picker but nothing has actually been saved: the server resolves it
    # straight to the library's own first collection.
    await default_library(db, library_id="1", server_title="Movies (4K HDR)", name="Movies")
    await signed_in(client, db)
    shelf_id = (await current_config(db)).home_shelves[0]["id"]

    with respx.mock:
        collections_route(
            "1",
            {"ratingKey": "99", "title": "Best of 2026"},
            {"ratingKey": "100", "title": "Worst of 2026"},
        )
        response = await client.post(
            f"/admin/libraries/home/shelves/{shelf_id}", data={"source": "collections:1"}
        )

    assert response.status_code == 200
    # The row now shows a live picker with that same collection selected.
    assert '<option value="col:1:99"\n              selected>' in response.text
    shelf = (await current_config(db)).home_shelves[0]
    assert shelf["path"] == "/library/collections/99/children"
    assert shelf["title"] == "Best of 2026"
    assert shelf["description"] == "Movies (4K HDR): Items in Best of 2026"


async def test_a_library_with_no_collections_cannot_be_switched_to_collection_items(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config
) -> None:
    await default_library(db, library_id="1", server_title="Movies (4K HDR)")
    await signed_in(client, db)
    shelf_id = (await current_config(db)).home_shelves[0]["id"]

    with respx.mock:
        collections_route("1")
        response = await client.post(
            f"/admin/libraries/home/shelves/{shelf_id}", data={"source": "collections:1"}
        )

    assert response.status_code == 400


async def test_switching_to_collection_items_without_a_plex_connection_is_rejected(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await default_library(db, library_id="1", server_title="Movies (4K HDR)")
    await signed_in(client, db)
    shelf_id = (await current_config(db)).home_shelves[0]["id"]

    response = await client.post(
        f"/admin/libraries/home/shelves/{shelf_id}", data={"source": "collections:1"}
    )

    assert response.status_code == 400


async def test_a_collection_shelf_shows_a_live_picker_on_every_render(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config
) -> None:
    # A saved collection shelf's picker is rebuilt from its own stored
    # fields on every render, not just right after it was chosen — so a
    # plain page load shows it too, already scoped to the right library and
    # collection, and its closed Content dropdown reads just the library's
    # own name rather than the full "library: collection" description every
    # other source shows there.
    await default_library(db, library_id="1", server_title="Movies (4K HDR)", name="Movies")
    await signed_in(client, db)
    shelf_id = (await current_config(db)).home_shelves[0]["id"]

    with respx.mock:
        collections_route("1", {"ratingKey": "99", "title": "Best of 2026"})
        respx.get(f"{PLEX_SERVER_URL}/library/sections").mock(
            return_value=httpx.Response(200, json=sections_payload(movies_section()))
        )
        await client.post(
            f"/admin/libraries/home/shelves/{shelf_id}",
            data={"source": "col:1:99", "collection_title": "Best of 2026"},
        )

        response = await client.get("/admin/libraries")

    assert response.status_code == 200
    assert '<option value="col:1:99"\n              selected>' in response.text
    assert '<option value="collections:1" hidden selected>Movies</option>' in response.text


async def test_editing_a_collection_shelfs_style_does_not_reset_its_collection(
    client: httpx.AsyncClient, db: AsyncSession, connected: Config
) -> None:
    # A Style/"Hide release year" edit resubmits the row's whole form,
    # collections picker included — without recognising that as "no source
    # change", it would look identical to a fresh switch and silently pick
    # the library's first collection again, discarding whichever one was
    # actually chosen.
    await default_library(db, library_id="1", server_title="Movies (4K HDR)", name="Movies")
    await signed_in(client, db)
    shelf_id = (await current_config(db)).home_shelves[0]["id"]

    with respx.mock:
        collections_route(
            "1",
            {"ratingKey": "99", "title": "Best of 2026"},
            {"ratingKey": "100", "title": "Worst of 2026"},
        )
        await client.post(
            f"/admin/libraries/home/shelves/{shelf_id}",
            data={"source": "col:1:100", "collection_title": "Worst of 2026"},
        )

        response = await client.post(
            f"/admin/libraries/home/shelves/{shelf_id}",
            data={"source": "collections:1", "style": "card"},
        )

    assert response.status_code == 200
    shelf = (await current_config(db)).home_shelves[0]
    assert shelf["path"] == "/library/collections/100/children"
    assert shelf["title"] == "Worst of 2026"
    assert shelf["style"] == "card"


async def test_choosing_a_collection_applies_it(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await default_library(db, library_id="1", server_title="Movies (4K HDR)", name="Movies")
    await signed_in(client, db)
    shelf_id = (await current_config(db)).home_shelves[0]["id"]

    response = await client.post(
        f"/admin/libraries/home/shelves/{shelf_id}",
        data={"source": "col:1:99", "collection_title": "Best of 2026"},
    )

    assert response.status_code == 200
    shelf = (await current_config(db)).home_shelves[0]
    assert shelf["path"] == "/library/collections/99/children"
    assert shelf["discoverHubKey"] is None
    assert shelf["description"] == "Movies (4K HDR): Items in Best of 2026"
    assert shelf["title"] == "Best of 2026"
    assert shelf["style"] == "poster"
    assert shelf["titleOnly"] is False


async def test_choosing_a_collection_without_a_title_is_rejected(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    shelf_id = (await current_config(db)).home_shelves[0]["id"]

    response = await client.post(
        f"/admin/libraries/home/shelves/{shelf_id}",
        data={"source": "col:1:99", "collection_title": ""},
    )

    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# Carousel
# --------------------------------------------------------------------------- #


async def test_enabling_the_carousel_switch_writes_through(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    config = await get_config(db)
    config.home_carousel_enabled = False
    await db.commit()

    response = await client.post(
        "/admin/libraries/home/carousel-enabled", data={"enabled": "on"}
    )

    assert response.status_code == 200
    assert "checked" in response.text
    assert (await current_config(db)).home_carousel_enabled is True


async def test_disabling_the_carousel_switch(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.post("/admin/libraries/home/carousel-enabled", data={})

    assert response.status_code == 200
    assert "checked" not in response.text
    assert (await current_config(db)).home_carousel_enabled is False


async def test_a_fresh_config_starts_with_a_continue_watching_carousel(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Seeded by ensure_default_carousel at app startup — the Carousel is a
    # shelf-shaped piece of Home config like any other, so it's never left
    # unconfigured for a fresh install to stumble on as an empty picker.
    config = await get_config(db)
    assert config.home_carousel is not None
    assert config.home_carousel["path"] == "/library/onDeck"


async def test_setting_the_carousel_source(client: httpx.AsyncClient, db: AsyncSession) -> None:
    await default_library(db, library_id="1", server_title="Movies")
    await signed_in(client, db)

    # No redirect: the response is the updated Carousel section, swapped in
    # place, same as every other write on this page.
    response = await client.post("/admin/libraries/home/carousel", data={"source": "lib:1:all"})

    assert response.status_code == 200
    assert 'id="carousel-section"' in response.text
    carousel = (await current_config(db)).home_carousel
    assert carousel["path"] == "/library/sections/1/all"


async def test_disabling_the_carousel_switch_keeps_its_configured_content(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # There is no longer an "off" source — the enabled switch is the only
    # on/off control, and it never clears what's actually configured: it
    # just hides the row (see partials/carousel.html), so turning it back on
    # brings back exactly what was there.
    await default_library(db, library_id="1", server_title="Movies")
    await signed_in(client, db)
    await client.post("/admin/libraries/home/carousel", data={"source": "lib:1:all"})

    response = await client.post("/admin/libraries/home/carousel-enabled", data={})

    assert response.status_code == 200
    # The row itself is gone from the response, not just visually dimmed —
    # the switch's own toggle now swaps the whole Carousel section, so a
    # narrower swap can no longer leave it stale.
    assert 'id="shelf-carousel"' not in response.text
    config = await current_config(db)
    assert config.home_carousel_enabled is False
    assert config.home_carousel["path"] == "/library/sections/1/all"


async def test_re_enabling_the_carousel_switch_shows_its_row_again(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await default_library(db, library_id="1", server_title="Movies")
    await signed_in(client, db)
    await client.post("/admin/libraries/home/carousel", data={"source": "lib:1:all"})
    await client.post("/admin/libraries/home/carousel-enabled", data={})

    response = await client.post(
        "/admin/libraries/home/carousel-enabled", data={"enabled": "on"}
    )

    assert response.status_code == 200
    assert 'id="shelf-carousel"' in response.text
    config = await current_config(db)
    assert config.home_carousel_enabled is True
    assert config.home_carousel["path"] == "/library/sections/1/all"


async def test_editing_the_carousel_without_changing_its_source(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)

    response = await client.post(
        "/admin/libraries/home/carousel",
        data={"source": "ondeck", "title": "My Carousel", "style": "poster"},
    )

    assert response.status_code == 200
    carousel = (await current_config(db)).home_carousel
    assert carousel["title"] == "My Carousel"
    assert carousel["path"] == "/library/onDeck"


# --------------------------------------------------------------------------- #
# Top Shelf
# --------------------------------------------------------------------------- #


async def test_a_fresh_config_starts_with_a_continue_watching_top_shelf(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Seeded by ensure_default_top_shelf at app startup, same as the shelf
    # list and the Carousel — there is no enabled switch to leave it behind,
    # so it always has to be *something*.
    config = await get_config(db)
    assert config.home_top_shelf is not None
    assert config.home_top_shelf["path"] == "/library/onDeck"


async def test_setting_the_top_shelf_source(client: httpx.AsyncClient, db: AsyncSession) -> None:
    await default_library(db, library_id="1", server_title="Movies")
    await signed_in(client, db)

    response = await client.post(
        "/admin/libraries/home/top-shelf", data={"source": "lib:1:newest"}
    )

    assert response.status_code == 200
    assert 'id="top-shelf-section"' in response.text
    top_shelf = (await current_config(db)).home_top_shelf
    assert top_shelf["path"] == "/library/sections/1/newest"


async def test_editing_the_top_shelf_without_changing_its_source(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)

    response = await client.post(
        "/admin/libraries/home/top-shelf",
        data={"source": "ondeck", "title": "My Top Shelf", "style": "poster"},
    )

    assert response.status_code == 200
    top_shelf = (await current_config(db)).home_top_shelf
    assert top_shelf["title"] == "My Top Shelf"
    assert top_shelf["path"] == "/library/onDeck"


async def test_there_is_no_enabled_switch_for_the_top_shelf(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    await signed_in(client, db)
    response = await client.get("/admin/libraries")

    assert response.status_code == 200
    assert "top-shelf-enabled" not in response.text


async def test_the_carousel_and_top_shelf_keep_their_own_content_on_the_same_page(
    client: httpx.AsyncClient, db: AsyncSession
) -> None:
    # Carousel and Top Shelf are both one "shelf row" and share every field
    # name in their own context dicts — a page render that flattened both
    # into the same namespace would have the second one silently clobber
    # the first's post_url/current/etc.
    await default_library(db, library_id="1", server_title="Movies")
    await signed_in(client, db)
    await client.post("/admin/libraries/home/carousel", data={"source": "lib:1:all"})
    await client.post("/admin/libraries/home/top-shelf", data={"source": "lib:1:newest"})

    response = await client.get("/admin/libraries")

    assert response.status_code == 200
    assert 'action="/admin/libraries/home/carousel"' in response.text
    assert 'action="/admin/libraries/home/top-shelf"' in response.text
    # Each row's own form still points at its own endpoint, not the other's.
    assert response.text.count('action="/admin/libraries/home/carousel"') == 2
    assert response.text.count('action="/admin/libraries/home/top-shelf"') == 2


async def test_toggling_include_on_deck(client: httpx.AsyncClient, db: AsyncSession) -> None:
    await signed_in(client, db)

    response = await client.post(
        "/admin/libraries/home/carousel-include-on-deck", data={"enabled": "on"}
    )

    assert response.status_code == 200
    assert (await current_config(db)).home_carousel_include_on_deck is True
