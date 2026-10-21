"""Playback events: what Canopy+'s player is doing, forwarded to the admin's webhooks.

Canopy+ has a built-in video player, and the player knows things Plex's own
webhooks never say: which chapter just started, when the intro begins and ends,
when the credits roll. Each Apple TV posts those moments to
``POST /playback/events``, tagged with a *room* the user typed into the app, and
this service forwards each one, as JSON, to every URL the admin has listed on
the Playback tab. The intended other end is a home automation — a Home
Assistant webhook trigger that dims the living-room lights on ``started`` and
brings them back up on ``credits_started``.

Three decisions shape the whole feature:

**Webhooks, configured here, and nothing else.** No MQTT, no Home Assistant
integration, no per-room routing. Every event goes to every URL; the automation
at the other end decides what a room means. One format, written down on the
Playback tab, is easier to automate against than a set of integrations.

**The app never waits.** The endpoint validates, records the event in memory,
answers 202 and forwards afterwards. A webhook that is slow, down or answering
500 is logged and shown on the Playback tab, and is never the Apple TV's
problem — a player that stuttered because a light bulb's hub was rebooting
would be a much worse bug than a missed automation.

**Nothing is stored.** The last few dozen events and each webhook's last
result live in memory (:class:`~cplus_service.playback.forward.PlaybackLog`,
hung off :class:`~cplus_service.api.state.AppState`) for the admin to look at
while setting an automation up. A restart forgets them, and that is fine: a
history of who paused what is not something this service has any reason to
keep.

Modules:

* :mod:`.events` — the request body Canopy+ posts, and the body forwarded on.
* :mod:`.forward` — delivery to the webhooks, and the in-memory log.
"""
