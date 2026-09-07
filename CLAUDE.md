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

**Script-context gotcha:** `indigo-host -e` scripts and the scripting shell
always see plugin-owned `pluginProps` as **empty** (`{}`) and are refused
writes (`replacePluginPropsOnServer` raises InvalidParameter outside the
owning plugin). So (a) never "verify" this plugin's props from a script —
an empty dict there means nothing — and (b) device renames via
`dev.replaceOnServer()` from scripts are safe: the server preserves the
real props (verified live 2026-07-05). As defense-in-depth the plugin
self-heals anyway: `_repair_orphan_devices` re-links any roomieRoom device
with a genuinely missing `roomUuid` from its `roomName` state on each poll.

**First-time install** (learned during the initial deploy): the deploy script
only *updates* plugin files — the Indigo server does not rescan the Plugins
folder for new bundles while running, and the IOM has no enable API. To
register a brand-new plugin, `open` the bundle on the Mac
(`open ".../Plugins/Roomie Remote.indigoPlugin"`) and click **Install and
Enable** in the Indigo client dialog (GUI-automatable via AppleScript/
Peekaboo). If the client shows "Server Connection Status / Disconnected",
connect it first via *Connect to Remote Server...* → `127.0.0.1`. Plugin
prefs can be pre-seeded by writing
`.../Preferences/Plugins/com.vtmikel.roomie.indiPref` **before** enabling;
once the plugin is running the server owns that file, so change settings via
the plugin's Configure dialog instead.

### Poking the Roomie API directly
```bash
curl "http://<roomie-controller>:47147/api/v1/rooms" | python3 -m json.tool
curl "http://<roomie-controller>:47147/api/v1/activities" | python3 -m json.tool
```

## Architecture

```
Roomie Remote.indigoPlugin/Contents/
  Resources/mcp-manifest.json   # MCP Server provider manifest (8 roomie_* tools)
  Server Plugin/
    plugin.py        # Indigo entry point: lifecycle, ConfigUIs, actions, menus,
                     # automatic room-device creation, poll-outcome application,
                     # handle_mcp_tool_invoke + IndigoBridge for the MCP tools
    roomie/          # Indigo-free core (NO indigo imports — unit-testable)
      client.py      # RoomieClient: HTTP + envelope parsing + typed exceptions
      models.py      # Room/Activity dataclasses; tolerant, presence-tested parsing
      poller.py      # RoomiePoller: diffing, backoff, caches, wake event
      lexicon.py     # The 78-button Universal Remote lexicon + legacy aliases
    mcp_api/         # Indigo-free MCP tool provider (NO indigo imports)
      tool_dispatcher.py  # name -> handler table, JSON envelope, ToolError mapping
      tool_service.py     # RoomieToolService: the 8 tools; IndigoBridge callables
      resolvers.py        # room / activity resolution (name | UUID | Indigo id)
      errors.py           # ToolError(validation|not_found|conflict|internal)
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
- **One button lexicon.** `roomie/lexicon.py` is the only list of remote
  button names; the Press Remote Button menu and the MCP `press_button` enum
  are both generated from it (`tests/test_manifest_sync.py` fails on drift).
  `normalize_button` maps the pre-2026.9.0 names (`Mute`, `SkipBack`,
  `0`–`9`, `Input`) to lexicon names so saved actions keep working. If Roomie
  bumps `lexicon_version` (visible in `/remote/capabilities`), extend the
  lexicon and regenerate the manifest enum.

## MCP tool provider

The plugin contributes tools to the Indigo MCP Server plugin
(`com.vtmikel.mcp_server` ≥ 2026.8.1) via its provider contract
(`docs/mcp-provider-manifest.md` in the `indigo-mcp-server` repo; Auto Lights
is the other provider). Three pieces: `Contents/Resources/mcp-manifest.json`
(the tool list + JSON Schemas, served to AI clients verbatim), the hidden
`mcp_tool_invoke` action in `Actions.xml` → `Plugin.handle_mcp_tool_invoke`,
and the `mcp_tools_updated` broadcast in `startup()`.

- **Dispatch shape.** The MCP Server calls
  `executeAction("mcp_tool_invoke", props={"tool": <bare name>, "arguments": <JSON string>}, waitUntilDone=True)`
  and expects a JSON-string envelope back — never a raised exception.
  Arguments cross as a JSON string because `indigo.Dict` cannot hold `null`.
- **Lazy import.** `mcp_api` is imported inside `handle_mcp_tool_invoke` on
  first use, so tool bugs cannot affect startup for users without the MCP
  Server. `_indigo_bridge()` is the only place the tools touch Indigo: it
  hands `RoomieToolService` callables for the device registries (under
  `_dev_lock`) and `_schedule_refresh`.
- **Threading.** Handlers run on the plugin's callback thread (serialized
  with ConfigUI callbacks and actions). Reads default to poller caches; live
  tools make at most two bounded HTTP calls, each capped by the request
  timeout pref. Manifest timeouts: cache reads 15 s, live reads 20 s, writes
  30 s.
- **Writes return immediately** and call `_schedule_refresh()` (same
  ~1.5 s wake the actions use) so device states converge; results carry a
  `note` telling the AI to confirm with `roomie_get_room`.
- **Tool naming.** The MCP Server enforces the `roomie_` prefix; through the
  homelab ContextForge gateway the same tools surface as `indigo-roomie-…`.
  Adding a tool = add the method to `RoomieToolService`, the table entry in
  `ToolDispatcher`, the manifest entry, and a `tests/test_mcp_tools.py` case.

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
- `POST /remote/press` — `{"button": <lexicon name>, "roomuuid": ...,
  "activityuuid": <override>, "action": "tap"|"press"|"release"|"repeat",
  "count": n (tap burst, max 25), "digits": "..." (Channel only),
  "hold_ms": n (press only; server auto-releases), "session": ... (release)}`.
  Roomie resolves the button against the room's current activity. Reply `da`:
  `{button, role, deviceuuid, command, dispatched[, session]}`.
  Errors: 400 unknown button, 404 unknown room/activity, 409 no activity
  running, 422 button unsupported in the current activity.
- `GET /remote/capabilities?roomuuid=|roomname=|activityuuid=` — resolution
  for **every** lexicon button (`null` = unsupported) with `category`, `role`,
  `command`/`deviceuuid` or `activityuuid`/`activityname`, plus
  `lexicon_version` (1 as of 2026-09). The docs say 409 when the room is off;
  the live controller instead resolves against "System Off" and reports only
  the activity buttons as supported (verified 2026-09-07) — the tool handles
  both. This is the authoritative button list — `roomie/lexicon.py` was
  captured from it.
- `GET /devices` — every device Roomie knows (86 live), most of them Indigo
  devices imported via HomeKit with an empty `address`; only network AV gear
  carries `address`/`port`.
- **No configuration editing.** The API is read + execute only (GET/POST;
  no create/update/delete for rooms, activities or devices).
- The production controller is the Roomie iPad on the switch (`gi20`) at
  `roomie-controller.home.mikelamoureux.net` → `10.66.0.54` (the plugin is
  configured with the hostname); it must have Roomie foregrounded for the API
  to answer, and the plugin log shows occasional 5 s read timeouts from it.
  Second power-off of an already-off room returns success (not the
  documented 409) — the 409 handling in `power_off_room` is defensive.

## Testing conventions

- All tests live in `tests/`; `tests/conftest.py` installs the indigo stub
  into `sys.modules` **before** adding `Server Plugin` to `sys.path`.
- `test_client.py` injects `FakeSession` (scripted responses + recorded
  calls); `test_poller.py`/`test_plugin.py` inject `FakeClient`.
- `test_mcp_tools.py` drives `ToolDispatcher.dispatch()` exactly like
  `handle_mcp_tool_invoke` (JSON in, JSON envelope out) with its own richer
  `FakeClient` and an `IndigoBridge` of lambdas; `test_manifest_sync.py` is
  the manifest ↔ dispatcher ↔ lexicon ↔ Info.plist drift guard;
  `TestMcpProviderWiring` in `test_plugin.py` covers the real plugin entry
  point through the indigo stub (which now records `indigo.server`
  broadcasts in `tests.conftest.broadcasts`).
- The stub `Device` records `updateStatesOnServer` / `setErrorStateOnServer` /
  `updateStateImageOnServer` calls for assertions; `indigo.device.create`
  appends to `tests.conftest.created_devices`.
- Live fixture data in `tests/test_models.py` mirrors real captures from the
  production controller — keep it realistic when adding cases.

## Versioning

`Info.plist` `PluginVersion` uses CalVer (`YYYY.N.N`), matching the other
vtmikel plugins. Bump it in any PR that changes plugin behavior.
