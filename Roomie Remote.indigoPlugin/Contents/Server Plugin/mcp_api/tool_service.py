"""Tool implementations behind the MCP `mcp_tool_invoke` action.

Indigo-free: everything the tools need from Indigo (device registries, the
post-write poller refresh) arrives through an injected IndigoBridge of
callables, so this module is unit-testable exactly like `roomie/`.

Reads default to the poller's caches (no HTTP); `refresh=true` and the
capabilities/devices/status-test tools talk to the controller live. Writes
dispatch to Roomie, schedule the same short poller refresh the plugin's own
actions use, and return immediately.

Failures are raised as ToolError (validation | not_found | conflict |
internal) and become the in-band error envelope in the dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from roomie.client import (
    RoomieError,
    RoomieRequestFailed,
    RoomieServerError,
    RoomieUnreachable,
)
from roomie.lexicon import (
    BUTTON_CATEGORY_BY_NAME,
    BUTTON_NAMES,
    LEXICON_VERSION,
    UnknownButton,
    normalize_button,
)
from roomie.models import Room

from .errors import ToolError
from .resolvers import resolve_activity, resolve_room

UNREACHABLE_HINT = (
    "Roomie must be running in the foreground on the Primary Controller with "
    "Local Network Control enabled"
)
CONFIRM_HINT = (
    "Indigo device states converge within a few seconds; confirm with roomie_get_room"
)
MAX_TAPS = 25
MAX_HOLD_MS = 10000
MIN_DELAY_SECONDS = 1.0

# Button categories whose press changes which activity is running.
_STATE_CHANGING_CATEGORIES = {"activity", "power"}


def _no_ids(room_uuid):
    return []


def _no_uuid(dev_id):
    return None


def _no_refresh():
    return None


def _no_counts():
    return (0, 0)


@dataclass
class IndigoBridge:
    """Indigo-side lookups injected by plugin.py (all optional for tests)."""

    room_device_ids: Callable[[str], list] = _no_ids
    activity_devices: Callable[[str], list] = _no_ids
    room_uuid_for_device: Callable[[int], Optional[str]] = _no_uuid
    schedule_refresh: Callable[[], None] = _no_refresh
    device_counts: Callable[[], tuple] = _no_counts


class RoomieToolService:
    def __init__(
        self,
        poller,
        client_factory: Callable[[], Any],
        bridge: Optional[IndigoBridge] = None,
        plugin_version: str = "",
        prefs=None,
    ):
        self._poller = poller
        self._client_factory = client_factory
        self._bridge = bridge or IndigoBridge()
        self._plugin_version = plugin_version
        self._prefs = prefs if prefs is not None else {}
        # uuid -> name, filled whenever /devices is fetched; lets press results
        # name the device without an extra round trip.
        self._device_names: dict[str, str] = {}

    ########################################
    # Read tools
    ########################################

    def list_rooms(self, refresh=False):
        refresh = self._as_bool(refresh, "refresh")
        rooms = self._rooms(refresh)
        return {
            "controller": self._controller_summary(),
            "rooms": [
                self._room_summary(room)
                for room in sorted(rooms, key=lambda r: r.name.lower())
            ],
        }

    def get_room(self, room, refresh=False):
        refresh = self._as_bool(refresh, "refresh")
        target = self._resolve_room(self._rooms(refresh), room)
        extras = self._poller.activities_for_room(target.uuid)
        current = target.current_activity_uuid
        payload = self._room_summary(target)
        payload.update(
            {
                "activities": [
                    {
                        "uuid": a.uuid,
                        "name": a.name,
                        "icon": a.icon,
                        "is_current": bool(current) and a.uuid == current,
                    }
                    for a in target.activities
                ],
                "toggle_activities": [
                    {
                        "uuid": a.uuid,
                        "base_uuid": a.base_uuid,
                        "name": a.name,
                        "toggle_state": a.toggle_state,
                    }
                    for a in extras
                    if a.toggle_state is not None
                ],
                "off_activities": [
                    {"uuid": a.uuid, "name": a.name} for a in extras if a.is_off_type
                ],
                "indigo_activity_devices": self._bridge.activity_devices(target.uuid),
                "last_poll": self._poller.last_successful_poll,
            }
        )
        return payload

    def get_capabilities(self, room, activity=None):
        target = self._resolve_room(self._rooms(False), room)
        client = self._client_factory()
        if activity is not None and str(activity).strip():
            chosen, _ = resolve_activity(
                target, self._poller.activities_for_room(target.uuid), activity
            )
            scope = {"activityuuid": chosen.uuid}
        else:
            scope = {"roomuuid": target.uuid}
        data = self._call(
            client.get_capabilities,
            on_code={
                409: (
                    "conflict",
                    f"Room '{target.name}' is off, so there is no running activity "
                    "to resolve buttons against; start one with "
                    "roomie_start_activity, or pass activity to evaluate a "
                    "specific activity",
                ),
                404: (
                    "not_found",
                    f"Roomie did not recognise the room/activity for "
                    f"'{target.name}'; refresh with roomie_list_rooms(refresh=true)",
                ),
            },
            **scope,
        )
        data = data if isinstance(data, dict) else {}
        self._load_device_names(client)

        supported: dict[str, list] = {}
        unsupported = []
        for name, info in (data.get("buttons") or {}).items():
            if not info:
                unsupported.append(name)
                continue
            category = info.get("category") or BUTTON_CATEGORY_BY_NAME.get(
                name, "other"
            )
            entry = {"button": name, "role": info.get("role")}
            for key, out in (
                ("command", "command"),
                ("deviceuuid", "device_uuid"),
                ("activityuuid", "activity_uuid"),
                ("activityname", "activity_name"),
            ):
                if info.get(key) is not None:
                    entry[out] = info[key]
            device_name = self._device_names.get(entry.get("device_uuid"))
            if device_name:
                entry["device_name"] = device_name
            supported.setdefault(category, []).append(entry)

        result = {
            "room_uuid": data.get("roomuuid", target.uuid),
            "room_name": data.get("roomname", target.name),
            "activity_uuid": data.get("activityuuid"),
            "activity_name": data.get("activityname"),
            "lexicon_version": data.get("lexicon_version"),
            "supported": supported,
            "unsupported": sorted(unsupported),
        }
        reported = data.get("lexicon_version")
        if reported not in (None, LEXICON_VERSION):
            result["note"] = (
                f"Controller reports lexicon_version {reported}; this plugin knows "
                f"version {LEXICON_VERSION}, so Roomie may accept buttons not in "
                "the press_button enum"
            )
        return result

    def list_devices(self, room=None, include_all=False):
        include_all = self._as_bool(include_all, "include_all")
        room_uuid = None
        if room is not None and str(room).strip():
            room_uuid = self._resolve_room(self._rooms(False), room).uuid
        client = self._client_factory()
        devices = self._call(client.get_devices)
        devices = [d for d in (devices or []) if isinstance(d, dict)]
        self._remember_device_names(devices)

        entries = []
        for device in devices:
            if room_uuid and device.get("roomuuid") != room_uuid:
                continue
            address = device.get("address") or ""
            if not include_all and not address:
                continue
            entries.append(
                {
                    "uuid": device.get("uuid"),
                    "name": device.get("name"),
                    "brand": device.get("brand"),
                    "model": device.get("model"),
                    "type": device.get("type"),
                    "room_uuid": device.get("roomuuid"),
                    "room_name": device.get("roomname"),
                    "address": address or None,
                    "port": device.get("port") or None,
                }
            )
        return {
            "devices": entries,
            "count": len(entries),
            "total_in_roomie": len(devices),
            "filter": "all" if include_all else "network_devices_only",
        }

    def get_status(self, test_connection=False):
        test_connection = self._as_bool(test_connection, "test_connection")
        failures = self._poller.consecutive_failures
        last = self._poller.last_successful_poll
        room_devices, activity_devices = self._bridge.device_counts()
        result = {
            "plugin_version": self._plugin_version,
            "host": self._prefs.get("host", ""),
            "port": int(self._prefs.get("port") or 47147),
            "poll_interval_seconds": float(self._prefs.get("pollInterval") or 5),
            "request_timeout_seconds": float(self._prefs.get("requestTimeout") or 5),
            "reachable": failures == 0 and last is not None,
            "consecutive_failures": failures,
            "current_backoff_seconds": self._poller.next_delay(),
            "last_successful_poll": last,
            "rooms_cached": len(self._poller.rooms_snapshot()),
            "activities_cached": len(self._poller.activities_snapshot()),
            "indigo_room_devices": room_devices,
            "indigo_activity_devices": activity_devices,
            "lexicon_version": LEXICON_VERSION,
        }
        if not result["reachable"]:
            result["hint"] = UNREACHABLE_HINT
        if test_connection:
            try:
                rooms = self._client_factory().get_rooms()
                result["live_test"] = {
                    "ok": True,
                    "room_count": len(rooms),
                    "rooms": [r.get("roomname") for r in rooms if isinstance(r, dict)],
                }
            except RoomieError as exc:
                result["live_test"] = {
                    "ok": False,
                    "error": str(exc),
                    "hint": UNREACHABLE_HINT,
                }
        return result

    ########################################
    # Write tools
    ########################################

    def start_activity(self, room, activity, toggle_state=None, delay_seconds=None):
        if toggle_state is not None and toggle_state not in ("on", "off"):
            raise ToolError("validation", "toggle_state must be 'on' or 'off'")
        delay = None
        if delay_seconds is not None:
            if isinstance(delay_seconds, bool):
                raise ToolError("validation", "delay_seconds must be a number")
            try:
                delay = float(delay_seconds)
            except (TypeError, ValueError):
                raise ToolError("validation", "delay_seconds must be a number")
            if delay < MIN_DELAY_SECONDS:
                raise ToolError(
                    "validation",
                    f"delay_seconds must be at least {MIN_DELAY_SECONDS} (Roomie "
                    "rejects smaller delays); omit it for no delay",
                )

        target = self._resolve_room(self._rooms(False), room)
        chosen, ts = resolve_activity(
            target,
            self._poller.activities_for_room(target.uuid),
            activity,
            toggle_state,
        )
        running = target.current_activity_uuid
        was_already_running = bool(running) and running in (
            chosen.uuid,
            chosen.base_uuid,
        )

        client = self._client_factory()
        self._call(
            client.run_activity,
            chosen.uuid,
            ts=ts,
            delay=delay,
            on_code={
                404: (
                    "not_found",
                    f"Roomie no longer knows activity '{chosen.name}' "
                    f"({chosen.uuid}); it may have been deleted — refresh with "
                    "roomie_list_rooms(refresh=true)",
                )
            },
        )
        self._bridge.schedule_refresh()

        result = {
            "room": {"uuid": target.uuid, "name": target.name},
            "activity": {"uuid": chosen.uuid, "name": chosen.name},
            "dispatched": True,
            "was_already_running": was_already_running,
            "note": CONFIRM_HINT,
        }
        state = ts or chosen.toggle_state
        if state:
            result["activity"]["toggle_state"] = state
        if delay is not None:
            result["delay_seconds"] = delay
        if chosen.is_off_type:
            result["is_off_activity"] = True
        return result

    def power_off_room(self, room):
        target = self._resolve_room(self._rooms(False), room)
        was_already_off = bool(target.is_off)
        client = self._client_factory()
        dispatched = True
        try:
            self._call(client.press, "ActivityOff", target.uuid)
        except ToolError as exc:
            # Roomie documents 409 for "already off"; live controllers usually
            # just return success. Either way it is a no-op, not an error.
            if (exc.details or {}).get("roomie_code") == 409:
                dispatched = False
                was_already_off = True
            else:
                raise
        self._bridge.schedule_refresh()
        return {
            "room": {"uuid": target.uuid, "name": target.name},
            "dispatched": dispatched,
            "was_already_off": was_already_off,
            "note": CONFIRM_HINT,
        }

    def press_button(
        self, room, button, count=1, digits=None, hold_ms=None, activity=None
    ):
        try:
            canonical = normalize_button(button)
        except UnknownButton as exc:
            raise ToolError(
                "validation",
                f"{exc}; button must be one of the Roomie lexicon names",
                {"valid_buttons": BUTTON_NAMES},
            )
        count = self._as_int(count if count is not None else 1, "count", 1, MAX_TAPS)
        if hold_ms is not None:
            hold_ms = self._as_int(hold_ms, "hold_ms", 1, MAX_HOLD_MS)
            if count != 1:
                raise ToolError(
                    "validation",
                    "hold_ms and count > 1 are mutually exclusive: a held button "
                    "repeats on its own until released",
                )
        if digits is not None:
            digits = str(digits).strip()
            if not digits.isdigit():
                raise ToolError("validation", "digits must contain only 0-9")
            if canonical != "Channel":
                raise ToolError(
                    "validation", "digits only applies to the Channel button"
                )

        target = self._resolve_room(self._rooms(False), room)
        activityuuid = None
        override_name = None
        if activity is not None and str(activity).strip():
            chosen, _ = resolve_activity(
                target, self._poller.activities_for_room(target.uuid), activity
            )
            activityuuid = chosen.uuid
            override_name = chosen.name
        resolved_against = override_name or target.current_activity_name or "(off)"

        client = self._client_factory()
        data = self._call(
            client.press,
            canonical,
            target.uuid,
            count=count,
            digits=digits,
            action="press" if hold_ms is not None else None,
            hold_ms=hold_ms,
            activityuuid=activityuuid,
            on_code={
                409: (
                    "conflict",
                    f"No activity is running in '{target.name}', so '{canonical}' "
                    "has nothing to control; start one with roomie_start_activity",
                ),
                422: (
                    "conflict",
                    f"'{canonical}' is not supported by activity "
                    f"'{resolved_against}' in '{target.name}'; call "
                    "roomie_get_capabilities to see which buttons are",
                ),
                404: (
                    "not_found",
                    f"Roomie rejected the room/activity for '{target.name}' (404); "
                    "refresh with roomie_list_rooms(refresh=true)",
                ),
                400: (
                    "validation",
                    f"Roomie rejected button '{canonical}' (400); the controller's "
                    "lexicon may differ — check roomie_get_capabilities",
                ),
            },
        )
        data = data if isinstance(data, dict) else {}
        if BUTTON_CATEGORY_BY_NAME.get(canonical) in _STATE_CHANGING_CATEGORIES:
            self._bridge.schedule_refresh()

        result = {
            "room": {"uuid": target.uuid, "name": target.name},
            "button": canonical,
            "count": count,
            "dispatched": bool(data.get("dispatched", True)),
            "resolved_against": resolved_against,
        }
        for key, out in (
            ("role", "role"),
            ("command", "command"),
            ("deviceuuid", "device_uuid"),
            ("activityname", "activity_name"),
            ("session", "session"),
        ):
            if data.get(key) is not None:
                result[out] = data[key]
        if hold_ms is not None:
            result["hold_ms"] = hold_ms
        if digits:
            result["digits"] = digits
        device_name = self._device_names.get(result.get("device_uuid"))
        if device_name:
            result["device_name"] = device_name
        return result

    ########################################
    # Helpers
    ########################################

    def _rooms(self, refresh: bool) -> list[Room]:
        if refresh:
            self._call(self._poller.refresh_cache_now)
            self._poller.request_refresh()
            return self._poller.rooms_snapshot()
        rooms = self._poller.rooms_snapshot()
        if not rooms:
            # Nothing cached yet (plugin just started, or the controller was
            # down at startup): one bounded live attempt before giving up.
            self._call(self._poller.refresh_cache_now)
            rooms = self._poller.rooms_snapshot()
        return rooms

    def _resolve_room(self, rooms, room) -> Room:
        return resolve_room(rooms, room, self._bridge.room_uuid_for_device)

    def _room_summary(self, room: Room) -> dict:
        current = None
        if not room.is_off and room.current_activity_uuid:
            current = {
                "uuid": room.current_activity_uuid,
                "name": room.current_activity_name,
            }
        return {
            "room_uuid": room.uuid,
            "name": room.name,
            "is_on": not bool(room.is_off),
            "current_activity": current,
            "activity_count": len(room.activities),
            "indigo_device_ids": list(self._bridge.room_device_ids(room.uuid)),
        }

    def _controller_summary(self) -> dict:
        failures = self._poller.consecutive_failures
        last = self._poller.last_successful_poll
        return {
            "host": self._prefs.get("host", ""),
            "reachable": failures == 0 and last is not None,
            "last_poll": last,
            "consecutive_failures": failures,
        }

    def _load_device_names(self, client) -> None:
        try:
            devices = client.get_devices()
        except RoomieError:
            return
        self._remember_device_names(devices)

    def _remember_device_names(self, devices) -> None:
        for device in devices or []:
            if isinstance(device, dict) and device.get("uuid"):
                self._device_names[device["uuid"]] = (
                    device.get("name") or device["uuid"]
                )

    def _call(self, fn, *args, on_code=None, **kwargs):
        """Run a client/poller call, mapping Roomie failures to ToolError."""
        try:
            return fn(*args, **kwargs)
        except RoomieRequestFailed as exc:
            code = exc.status_code or exc.co
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = None
            if on_code and code in on_code:
                error_type, message = on_code[code]
                raise ToolError(error_type, message, {"roomie_code": code})
            default = {
                400: "validation",
                404: "not_found",
                409: "conflict",
                422: "conflict",
            }
            raise ToolError(
                default.get(code, "internal"),
                f"Roomie refused the request: {exc}",
                {"roomie_code": code},
            )
        except RoomieUnreachable as exc:
            raise ToolError(
                "internal", f"Roomie controller unreachable ({exc}). {UNREACHABLE_HINT}"
            )
        except RoomieServerError as exc:
            raise ToolError("internal", f"Roomie API error: {exc}")

    @staticmethod
    def _as_bool(value, name: str) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise ToolError("validation", f"{name} must be true or false")

    @staticmethod
    def _as_int(value, name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            raise ToolError(
                "validation",
                f"{name} must be an integer between {minimum} and {maximum}",
            )
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ToolError(
                "validation",
                f"{name} must be an integer between {minimum} and {maximum}",
            )
        if not minimum <= number <= maximum:
            raise ToolError(
                "validation", f"{name} must be between {minimum} and {maximum}"
            )
        return number
