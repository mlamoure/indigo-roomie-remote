# CLAUDE.md

This file provides guidance to Claude Code when working with the Roomie Remote
Indigo plugin. End-user documentation belongs in README.MD.

## Commands

### Testing
```bash
source .venv/bin/activate && python -m pytest
```
Tests stub the `indigo` module via `tests/conftest.py` (same approach as the
Auto Lights plugin) so they run anywhere. HTTP is never touched: tests inject
a `FakeSession` into `RoomieClient` or a `FakeClient` into `RoomiePoller`.

### Formatting
```bash
black .
```

### Environment setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Deploying to the production Indigo server
Always use the canonical deploy script (never scp/rsync plugin files manually):
```bash
# Linux dev box (ai-development)
/home/mike/programming/mike-local-development-scripts/deploy_indigo_plugin_to_server.sh \
    "Roomie Remote.indigoPlugin" /home/mike/programming/indigo-roomie-remote

# macOS dev machine
/Users/mike/Mike_Sync_Documents/Programming/mike-local-development-scripts/deploy_indigo_plugin_to_server.sh \
    "Roomie Remote.indigoPlugin" <repo-base-dir>
```
Then restart the plugin:
```bash
ssh mike@indigo.home.mikelamoureux.net 'bash -c "/usr/local/bin/indigo-restart-plugin com.vtmikel.roomie"'
```
Verify via the plugin's own log (not the global Events.txt):
`/Library/Application Support/Perceptive Automation/Indigo <version>/Logs/com.vtmikel.roomie/plugin.log`

### Poking the Roomie API directly
```bash
curl "http://<roomie-controller>:47147/api/v1/rooms" | python3 -m json.tool
curl "http://<roomie-controller>:47147/api/v1/activities" | python3 -m json.tool
```

## Architecture

```
Roomie Remote.indigoPlugin/Contents/Server Plugin/
  plugin.py        # Indigo entry point: lifecycle, ConfigUIs, actions, menus,
                   # automatic room-device creation, poll-outcome application
  roomie/          # Indigo-free core (NO indigo imports — unit-testable)
    client.py      # RoomieClient: HTTP + envelope parsing + typed exceptions
    models.py      # Room/Activity dataclasses; tolerant, presence-tested parsing
    poller.py      # RoomiePoller: diffing, backoff, caches, wake event
```

Rules that keep this maintainable:
- **`roomie/` never imports `indigo`.** All Indigo interaction lives in
  `plugin.py` (`_apply_outcome` is the only place device states are pushed).
- **Diff before update.** The poller emits only changed states per room,
  batched per device via `updateStatesOnServer([...])`. This is what makes
  Indigo triggers reliable (no redundant writes → no phantom trigger fires).
  `lastPoll` is intentionally exempt (emitted every successful poll).
- The poll loop runs in `runConcurrentThread` using sliced `self.sleep(0.5)`
  (honors plugin stop) plus a `threading.Event` for wake-early refreshes.
  Never replace this with `Event.wait()` — it would ignore `StopThread`.
- ConfigUI dynamic lists read the poller's caches only; the "Refresh Room
  List" button and menu items use `poller.refresh_cache_now()` (bounded,
  synchronous) when live data is needed.
- Device auto-creation: rooms are created as devices when first seen (UUID
  not in the `knownRoomUuids` plugin pref). Deleted devices stay deleted;
  the "Create Devices for All Rooms" menu item recreates unconditionally.

## Roomie Local Network Control API cheat sheet

- Base: `http://<controller>:47147/api/v1` — served by the Roomie app itself
  on the Primary Controller. **The API only answers while Roomie is running /
  foregrounded with Local Network Control enabled** (Roomie X 10.3+). If the
  port is closed, that's the first thing to check.
- Envelope on every response: `{"st": "success"|"fail"|"error", "da": <payload>, "co": <code>}`.
- `GET /rooms` — all rooms in one call (the poll). Room keys: `roomuuid`,
  `roomname`, `roomcolor` (int), `currentactivityuuid`, `currentactivityname`,
  `currentactivityoff` (present only when true), `activities` (embedded
  entries use `activityuuid`/`name`/`ic`; off-type activities not listed;
  rooms with nothing running may lack the currentactivity* keys entirely).
- `GET /activities` — entries use `uuid`, room-prefixed `name`, `icon`,
  `roomuuid`; optional `guideuuid`/`vuuid`/`openuuid`/`auxuuids`; off
  activities carry `"type": "off"`. Toggle activities appear twice with
  `+`/`-` UUID suffixes and "(On)"/"(Off)" names.
- `POST /runactivity` — `{"au": uuid, "ts": "on"|"off" (optional), "de": delay}`;
  `de` must be ≥ 1.0 seconds (API rejects smaller).
- `POST /remote/press` — `{"button": <symbolic>, "roomuuid": ..., "count": n, "digits": "..."}`.
  Roomie resolves symbolic buttons (`Play`, `VolumeUp`, `ActivityOff`,
  `Activity1..8`, ...) against the room's current activity.
  Errors: 404 unknown room/button, 409 no activity running, 422 button
  unsupported in the current activity.
- The production controller is the House iPad (`House-iPad` /
  see OPNsense DHCP reservation); it must have Roomie foregrounded.

## Testing conventions

- All tests live in `tests/`; `tests/conftest.py` installs the indigo stub
  into `sys.modules` **before** adding `Server Plugin` to `sys.path`.
- `test_client.py` injects `FakeSession` (scripted responses + recorded
  calls); `test_poller.py`/`test_plugin.py` inject `FakeClient`.
- The stub `Device` records `updateStatesOnServer` / `setErrorStateOnServer` /
  `updateStateImageOnServer` calls for assertions; `indigo.device.create`
  appends to `tests.conftest.created_devices`.
- Live fixture data in `tests/test_models.py` mirrors real captures from the
  production controller — keep it realistic when adding cases.

## Versioning

`Info.plist` `PluginVersion` uses CalVer (`YYYY.N.N`), matching the other
vtmikel plugins. Bump it in any PR that changes plugin behavior.
