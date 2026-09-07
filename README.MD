# Roomie Remote plugin for Indigo

Integrates [Roomie Remote](https://roomieremote.com) into
[Indigo](https://www.indigodomo.com) via Roomie's Local Network Control HTTP
API. Each Roomie **Room** becomes an Indigo device whose states track the
room's currently-running activity, and Indigo actions can start activities,
power rooms off, and send remote button presses.

## Features at a Glance

- One Indigo device per Roomie room, showing the current activity in the
  device list at a glance ("Watch Apple TV", "Off", ...).
- Optional relay-style **activity devices** (on = that activity is running;
  turn on/off to start the activity or power off the room).
- Automatic device creation: rooms get Indigo devices the first time they are
  seen (configurable, and deleted devices are not re-created behind your back).
- Actions: **Start Activity** (with activity picker), **Power Off Room**,
  **Press Remote Button** (full symbolic button set, repeat taps, channel
  digits), **Run Activity by UUID** (scripting escape hatch), **Refresh Status**.
- Trigger-friendly states: `currentActivityName`, `onOffState`,
  `controllerReachable` — with disciplined state updates so triggers only
  fire on real changes.
- Graceful degradation with exponential backoff when the Roomie controller is
  asleep or unreachable; devices show an "unreachable" error state.

## Requirements

- Indigo 2025.1 or later.
- Roomie Remote X **10.3 or later** with an active subscription that includes
  Local Network Control.
- **Local Network Control enabled** in Roomie's settings on the Primary
  Controller device.
- The Primary Controller must stay running with Roomie in the foreground —
  the API is served by the Roomie app itself. A dedicated always-on
  controller (e.g. an iPad in Guided Access / Single App Mode, or an Apple TV)
  is strongly recommended.
- A static IP or DHCP reservation for the controller is strongly recommended.

## Installation

1. Download the latest release and double-click `Roomie Remote.indigoPlugin`
   on your Indigo server, or install via the Indigo plugin store when
   available.
2. In the plugin config dialog, enter the Roomie controller's IP address
   (port stays `47147` unless you changed it in Roomie).
3. On the first successful connection the plugin creates an Indigo device for
   each Roomie room (in a "Roomie Remote" device folder).

## Plugin Configuration

| Setting | Default | Notes |
|---|---|---|
| Roomie Controller IP / hostname | — | The device running Roomie with Local Network Control enabled. |
| Port | 47147 | Rarely changed. |
| Poll interval | 5s | How often room status is refreshed (2–300s). Roomie has no push API, so state freshness equals this interval. |
| Request timeout | 5s | Per-HTTP-request timeout (1–30s). |
| Auto-create devices | on | Create an Indigo device for each newly-seen Roomie room. Deleted devices stay deleted. |
| Auto-create activity devices | off | Create a relay-style device for each newly-seen visible activity (one per activity per room). Deleted devices stay deleted; enabling later back-fills existing activities. |
| Event Logging Level | Info | Standard Indigo log-level menu. |

## Devices & States

**Roomie Room** (`roomieRoom`) — one per Roomie room.

| State | Type | Meaning |
|---|---|---|
| `onOffState` | boolean | true while a (non-off) activity is running. |
| `currentActivityUuid` | string | Empty when the room is off. |
| `currentActivityName` | string | Empty when off (UI shows "Off"). Primary trigger target. |
| `roomName` | string | Refreshed each poll (rooms can be renamed in Roomie). |
| `activityCount` | integer | Number of visible activities. |
| `activityList` | string | JSON array of `{uuid, name}` for scripting. |
| `controllerReachable` | boolean | false while polls are failing. |
| `lastPoll` | string | ISO timestamp of the last successful poll. |

A room whose current activity is an off-type ("System Off") is treated as
off: `onOffState` false, empty activity UUID/name. This keeps triggers simple
("became off") instead of matching magic activity names.

**Roomie Activity** (`roomieActivity`) — optional relay-style device bound to
one activity in one room; create them manually for the activities you care
about, or enable the auto-create-activity-devices checkbox to get one per
visible activity. The device is *on* while that activity is the room's
current activity.
Indigo's standard device controls map naturally: **Turn On** starts the
activity, **Turn Off** powers off the room, **Toggle** does what you'd
expect. This makes control pages and activity-level triggers trivial — no
state matching needed.

## Actions

- **Start Activity** — pick from the room's activities (toggle activities
  appear as separate "(On)"/"(Off)" entries). Optional delay (min 1.0s,
  a Roomie API requirement).
- **Power Off Room** — asks Roomie to resolve the correct off behavior for
  the room. A room that is already off is a no-op, not an error.
- **Press Remote Button** — Roomie's Universal Remote lexicon (78 symbolic
  buttons grouped by category: power, activity, volume, channel, transport,
  cursor, numeric, color, extended), with optional repeat taps (1–25) and
  channel digits. Buttons are resolved against the room's current activity
  by Roomie. Actions saved with the pre-2026.9.0 names (`Mute`, `SkipBack`,
  `0`–`9`, `Input`) keep working; `PowerOn`, `Dot`, `Eject`, `Search` and
  `Settings` had no Roomie equivalent and now log an error asking you to
  re-select the button.
- **Run Activity by UUID** — plugin-level action for scripting and edge cases
  (activities in rooms without an Indigo device). Supports the toggle-state
  override and delay.
- **Refresh Status** — force an immediate poll. Indigo's standard device
  "Status Request" does the same.

## Menu Items

- **Test Connection** — logs reachability and the room count.
- **Dump Rooms & Activities to Log** — pretty-prints every room and activity
  with UUIDs (toggle and off-type activities flagged). This is where you get
  UUIDs for *Run Activity by UUID*.
- **Create Devices for All Rooms** — creates Indigo devices for any rooms
  that don't have one (including ones you previously deleted).
- **Force Refresh All** — immediate poll with a full (non-diffed) state push.
- **Toggle Debug Logging** — temporary debug logging without opening the
  config dialog.

## MCP tools (AI assistants)

If the [Indigo MCP Server](https://github.com/mlamoure/indigo-mcp-server)
plugin (v2026.8.1 or later) is installed, this plugin contributes eight
`roomie_*` tools to it so AI assistants can see and control Roomie through
Indigo. Nothing changes for installations without the MCP Server: the
manifest is inert data and the tool code is only loaded when the MCP Server
calls it.

| Tool | Writes | What it does |
|---|---|---|
| `roomie_list_rooms` | no | Every room with on/off, current activity, and linked Indigo device ids (`refresh=true` fetches live). |
| `roomie_get_room` | no | One room in full: activities, toggle and off activities, linked Indigo room/activity devices. |
| `roomie_get_capabilities` | no | Which remote buttons the room's current activity supports, grouped by category, with the device each drives. |
| `roomie_list_devices` | no | Roomie's hardware inventory (network AV gear by default; `include_all=true` for everything). |
| `roomie_get_status` | no | Controller reachability, backoff, last poll, cache sizes; `test_connection=true` probes live. |
| `roomie_start_activity` | yes | Start an activity by name or UUID in a room (toggle activities take `toggle_state`). |
| `roomie_power_off_room` | yes | Power a room off; already-off is a reported no-op. |
| `roomie_press_button` | yes | Send a lexicon button: tap bursts (`count`), channel `digits`, held buttons (`hold_ms`), activity override. |

Rooms are addressed by name (case-insensitive, unique partial names work),
room UUID, or the Indigo id of the room's device; activities by name or UUID
within that room. Ambiguous names come back as an error listing the matches.
Write tools return immediately and Indigo device states catch up within a
poll; the MCP Server's "Allow plugin-provided tools to make changes" setting
gates them.

## Triggers

No custom trigger types are needed — standard Indigo device-state triggers
cover the use cases:

- "Living Room activity became *Watch Apple TV*" → state
  `currentActivityName` becomes value.
- "Any room turned off" → `onOffState` becomes false.
- "Controller went unreachable" → `controllerReachable` becomes false.

## Troubleshooting

- **"unreachable" on all devices / connection test fails**: the Roomie API
  only answers while the Roomie app is running (foregrounded) on the Primary
  Controller with Local Network Control enabled. Check
  `curl http://<controller>:47147/api/v1/rooms` from the Indigo server — if
  the port is closed, wake the controller / re-enable Local Network Control.
- **Room device shows "room not found"**: the room was deleted in Roomie (or
  the plugin is pointed at a different Roomie home). Delete the Indigo device
  or recreate the room.
- **Activity picker is empty**: the plugin hasn't reached the controller yet;
  use "Refresh Room List" / check the connection.

## Development

See `CLAUDE.md` for architecture and workflow. Tests: `python -m pytest`
(the `indigo` module is stubbed; no Indigo server needed).
