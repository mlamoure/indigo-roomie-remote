"""Roomie Remote plugin for Indigo.

Represents each Roomie room as an Indigo device whose states track the
room's currently-running activity, polled from Roomie's Local Network
Control API. See roomie/ for the Indigo-free client/poller core.
"""

import ipaddress
import json
import logging
import socket
import threading
import time

try:
    import indigo
except ImportError:
    pass

from roomie.client import RoomieClient, RoomieError, RoomieRequestFailed
from roomie.lexicon import (
    BUTTON_CATEGORIES,
    CATEGORY_LABELS,
    UnknownButton,
    normalize_button,
)
from roomie.poller import RoomiePoller

DEVICE_FOLDER_NAME = "Roomie Remote"
DEVICE_NAME_TEMPLATE = "Roomie Remote - {room}"
ACTIVITY_DEVICE_NAME_TEMPLATE = "Roomie Remote - {room} - {activity}"

# Symbolic buttons for the Press Remote Button menu come from the shared
# lexicon (roomie/lexicon.py) — the same list the MCP press_button tool uses.
# Roomie resolves them against the room's current activity; unsupported
# buttons return 409/422 at press time.
BUTTON_GROUPS = [
    (CATEGORY_LABELS[category], names) for category, names in BUTTON_CATEGORIES
]

SEPARATOR_VALUE = "-1"


class Plugin(indigo.PluginBase):
    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs)

        self.log_level = int(plugin_prefs.get("log_level", logging.INFO))
        self.indigo_log_handler.setLevel(self.log_level)
        self.plugin_file_handler.setLevel(logging.DEBUG)

        self._poller = None
        self._dev_lock = threading.Lock()
        self._devices_by_room = {}  # room_uuid -> set of roomieRoom device ids
        self._activity_devices_by_room = {}  # room_uuid -> set of roomieActivity ids
        self._force_full = False
        self._tool_dispatcher = None  # MCP provider, built lazily on first call

    ########################################
    # Lifecycle
    ########################################

    def startup(self):
        self.logger.debug("startup called")
        self._poller = RoomiePoller(
            self._build_client_from_prefs(),
            poll_interval=float(self.pluginPrefs.get("pollInterval", 5)),
        )
        # Tell the Indigo MCP Server (if installed) to re-read our
        # mcp-manifest.json. Harmless no-op when nobody subscribes.
        try:
            indigo.server.broadcastToSubscribers("mcp_tools_updated")
        except Exception as e:
            self.logger.debug(f"mcp_tools_updated broadcast failed: {e}")

    def shutdown(self):
        self.logger.debug("shutdown called")

    def runConcurrentThread(self):
        try:
            while True:
                if self.pluginPrefs.get("host"):
                    outcome = self._poller.poll_once(force_full=self._force_full)
                    self._force_full = False
                    self._apply_outcome(outcome)
                else:
                    self.logger.debug(
                        "no Roomie controller host configured; polling paused"
                    )
                deadline = time.monotonic() + self._poller.next_delay()
                while time.monotonic() < deadline:
                    self.sleep(0.5)
                    if self._poller.wake_event.is_set():
                        break
                self._poller.wake_event.clear()
        except self.StopThread:
            pass

    def deviceStartComm(self, dev):
        self.logger.debug(f"deviceStartComm: {dev.name}")
        dev.stateListOrDisplayStateIdChanged()
        room_uuid = dev.pluginProps.get("roomUuid")
        if not room_uuid:
            dev.setErrorStateOnServer("no room configured")
            return
        registry = (
            self._activity_devices_by_room
            if dev.deviceTypeId == "roomieActivity"
            else self._devices_by_room
        )
        with self._dev_lock:
            registry.setdefault(room_uuid, set()).add(dev.id)
        if dev.deviceTypeId == "roomieActivity":
            self._push_activity_state_from_cache(dev, room_uuid)
        else:
            self._push_full_state(dev, room_uuid)
        if self._poller:
            self._poller.request_refresh()

    def deviceStopComm(self, dev):
        self.logger.debug(f"deviceStopComm: {dev.name}")
        room_uuid = dev.pluginProps.get("roomUuid")
        registry = (
            self._activity_devices_by_room
            if dev.deviceTypeId == "roomieActivity"
            else self._devices_by_room
        )
        with self._dev_lock:
            if room_uuid in registry:
                registry[room_uuid].discard(dev.id)
                if not registry[room_uuid]:
                    del registry[room_uuid]

    ########################################
    # Config UIs
    ########################################

    def validatePrefsConfigUi(self, values_dict):
        errors = indigo.Dict()

        host = values_dict.get("host", "").strip()
        if not host:
            errors["host"] = "Enter the Roomie controller's IP address or hostname."
        else:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                try:
                    socket.getaddrinfo(host, None)
                except OSError:
                    errors["host"] = (
                        f"'{host}' is not an IP address or a resolvable hostname."
                    )

        port = self._validate_int(values_dict.get("port"), 1, 65535)
        if port is None:
            errors["port"] = "Port must be a number between 1 and 65535."

        if self._validate_int(values_dict.get("pollInterval"), 2, 300) is None:
            errors["pollInterval"] = "Poll interval must be between 2 and 300 seconds."

        if self._validate_float(values_dict.get("requestTimeout"), 1, 30) is None:
            errors["requestTimeout"] = (
                "Request timeout must be between 1 and 30 seconds."
            )

        if errors:
            return (False, values_dict, errors)

        # Live connectivity test: warn but never block the save, so the
        # plugin can be configured while the controller is offline.
        try:
            rooms = RoomieClient(
                host,
                port=port,
                timeout=float(values_dict.get("requestTimeout", 5)),
            ).get_rooms()
            self.logger.info(
                f"connection test OK: {host}:{port} reports {len(rooms)} room(s)"
            )
        except RoomieError as exc:
            self.logger.warning(
                f"connection test failed ({exc}); saving anyway — check that "
                "Local Network Control is enabled and Roomie is running on the controller"
            )
        return (True, values_dict)

    def closedPrefsConfigUi(self, values_dict, user_cancelled):
        if user_cancelled:
            return
        self.log_level = int(values_dict.get("log_level", logging.INFO))
        self.indigo_log_handler.setLevel(self.log_level)
        if self._poller:
            self._poller.poll_interval = float(values_dict.get("pollInterval", 5))
            self._poller.set_client(self._build_client_from_prefs(values_dict))
            self._poller.request_refresh()

    def validateDeviceConfigUi(self, values_dict, type_id, dev_id):
        errors = indigo.Dict()
        room_uuid = values_dict.get("roomUuid")
        if not room_uuid:
            errors["roomUuid"] = "Choose a room (use Refresh Room List if empty)."
            return (False, values_dict, errors)
        room = None
        for candidate in self._poller.rooms_snapshot() if self._poller else []:
            if candidate.uuid == room_uuid:
                room = candidate
                values_dict["roomName"] = candidate.name
                break
        if type_id == "roomieActivity":
            activity_uuid = values_dict.get("activityUuid")
            if not activity_uuid or activity_uuid == SEPARATOR_VALUE:
                errors["activityUuid"] = "Choose an activity."
                return (False, values_dict, errors)
            if room is not None:
                for activity in room.activities:
                    if activity.uuid == activity_uuid:
                        values_dict["activityName"] = activity.name
                        break
        return (True, values_dict)

    def validateActionConfigUi(self, values_dict, type_id, device_id):
        errors = indigo.Dict()
        if type_id in ("startActivity", "runActivityRaw"):
            if not values_dict.get("activityUuid", "").strip():
                errors["activityUuid"] = "Choose or enter an activity."
            delay = values_dict.get("delay", "").strip()
            if delay:
                try:
                    if float(delay) < 1.0:
                        raise ValueError
                except ValueError:
                    errors["delay"] = (
                        "Delay must be blank or a number of at least 1.0 "
                        "(the Roomie API rejects smaller delays)."
                    )
        elif type_id == "pressButton":
            button = values_dict.get("button", SEPARATOR_VALUE)
            if button == SEPARATOR_VALUE:
                errors["button"] = "Choose a button (separators are not buttons)."
            else:
                try:
                    normalize_button(button)
                except UnknownButton as exc:
                    errors["button"] = f"{exc}."
            count = values_dict.get("count", "").strip()
            if count and self._validate_int(count, 1, 25) is None:
                errors["count"] = "Taps must be between 1 and 25."
            digits = values_dict.get("digits", "").strip()
            if digits and not digits.isdigit():
                errors["digits"] = "Channel digits must be numeric."
        if errors:
            return (False, values_dict, errors)
        return (True, values_dict)

    ########################################
    # Dynamic lists
    ########################################

    def get_room_list(self, filter="", values_dict=None, type_id="", target_id=0):
        rooms = self._poller.rooms_snapshot() if self._poller else []
        entries = [(room.uuid, room.name) for room in rooms]
        entries.sort(key=lambda item: item[1].lower())
        saved_uuid = (values_dict or {}).get("roomUuid")
        if saved_uuid and saved_uuid not in {uuid for uuid, _ in entries}:
            saved_name = (values_dict or {}).get("roomName") or saved_uuid
            entries.insert(0, (saved_uuid, f"{saved_name} (cached)"))
        if not entries:
            entries = [("", "-- no rooms; check connection and refresh --")]
        return entries

    def refresh_room_list(self, values_dict, type_id, dev_id):
        """'Refresh Room List' button: one bounded live fetch into the cache."""
        try:
            rooms = self._poller.refresh_cache_now()
            self.logger.info(f"room list refreshed: {len(rooms)} room(s)")
        except RoomieError as exc:
            self.logger.warning(f"room list refresh failed: {exc}")
        return values_dict

    def get_activity_list(self, filter="", values_dict=None, type_id="", target_id=0):
        if not self._poller:
            return [("", "-- plugin still starting --")]
        # Device config dialogs carry the room in values_dict; action config
        # dialogs resolve it from the target device's props.
        room_uuid = (values_dict or {}).get("roomUuid")
        if not room_uuid:
            if not target_id:
                return [("", "-- choose a room first --")]
            try:
                room_uuid = indigo.devices[target_id].pluginProps.get("roomUuid")
            except KeyError:
                return [("", "-- device not found --")]
        if not room_uuid:
            return [("", "-- choose a room first --")]
        entries = []
        for room in self._poller.rooms_snapshot():
            if room.uuid == room_uuid:
                entries = [(a.uuid, a.name) for a in room.activities]
                break
        if filter != "visibleOnly":
            toggles = [
                a
                for a in self._poller.activities_for_room(room_uuid)
                if a.toggle_state is not None
            ]
            if toggles:
                entries.append((SEPARATOR_VALUE, "── Toggle Activities ──"))
                entries.extend((a.uuid, a.name) for a in toggles)
        if not entries:
            entries = [("", "-- no activities cached; is the controller online? --")]
        return entries

    def get_button_list(self, filter="", values_dict=None, type_id="", target_id=0):
        entries = []
        for group_name, buttons in BUTTON_GROUPS:
            entries.append((SEPARATOR_VALUE, f"── {group_name} ──"))
            entries.extend((button, button) for button in buttons)
        return entries

    ########################################
    # Actions
    ########################################

    def start_activity(self, action, dev, caller_waiting_for_result=False):
        uuid = action.props.get("activityUuid", "")
        delay = action.props.get("delay", "").strip()
        name = self._activity_name(uuid) or uuid
        try:
            self._client().run_activity(uuid, delay=float(delay) if delay else None)
            self.logger.info(f"{dev.name}: started activity '{name}'")
            self._schedule_refresh()
        except RoomieRequestFailed as exc:
            self.logger.error(
                f"{dev.name}: Roomie refused to start activity '{name}' ({uuid}): {exc}"
                " — the activity may have been deleted in Roomie; re-select it in the action config"
            )
        except RoomieError as exc:
            self.logger.error(f"{dev.name}: start activity '{name}' failed: {exc}")

    def power_off_room(self, action, dev, caller_waiting_for_result=False):
        room_uuid = dev.pluginProps.get("roomUuid")
        try:
            self._client().press("ActivityOff", room_uuid)
            self.logger.info(f"{dev.name}: powered off")
            self._schedule_refresh()
        except RoomieRequestFailed as exc:
            if exc.status_code == 409 or exc.co == 409:
                self.logger.info(
                    f"{dev.name}: no activity running; nothing to power off"
                )
            else:
                self.logger.error(f"{dev.name}: power off failed: {exc}")
        except RoomieError as exc:
            self.logger.error(f"{dev.name}: power off failed: {exc}")

    def press_button(self, action, dev, caller_waiting_for_result=False):
        try:
            button = normalize_button(action.props.get("button", ""))
        except UnknownButton as exc:
            self.logger.error(
                f"{dev.name}: {exc} — re-select the button in the action config"
            )
            return
        count = int(action.props.get("count") or 1)
        digits = action.props.get("digits", "").strip() or None
        room_uuid = dev.pluginProps.get("roomUuid")
        try:
            self._client().press(button, room_uuid, count=count, digits=digits)
            self.logger.debug(f"{dev.name}: pressed {button} x{count}")
        except RoomieRequestFailed as exc:
            code = exc.status_code or exc.co
            activity = dev.states.get("currentActivityName", "")
            if code == 409:
                self.logger.warning(
                    f"{dev.name}: cannot press '{button}' — no activity is running"
                )
            elif code == 422:
                self.logger.warning(
                    f"{dev.name}: button '{button}' is not supported by the "
                    f"current activity '{activity}'"
                )
            else:
                self.logger.error(f"{dev.name}: press '{button}' failed: {exc}")
        except RoomieError as exc:
            self.logger.error(f"{dev.name}: press '{button}' failed: {exc}")

    def run_activity_raw(self, action):
        uuid = action.props.get("activityUuid", "").strip()
        ts = action.props.get("ts", "default")
        delay = action.props.get("delay", "").strip()
        try:
            self._client().run_activity(
                uuid,
                ts=None if ts == "default" else ts,
                delay=float(delay) if delay else None,
            )
            self.logger.info(f"ran activity {uuid} (ts={ts})")
            self._schedule_refresh()
        except RoomieError as exc:
            self.logger.error(f"run activity {uuid} failed: {exc}")

    def refresh_status(self, action, dev, caller_waiting_for_result=False):
        self.logger.debug(f"refresh status requested for {dev.name}")
        self._poller.request_refresh()

    def actionControlGeneral(self, action, dev):
        if action.deviceAction == indigo.kUniversalAction.RequestStatus:
            self.logger.info(f"{dev.name}: status requested")
            self._poller.request_refresh()

    def actionControlDevice(self, action, dev):
        """Standard on/off/toggle for roomieActivity relay devices."""
        if dev.deviceTypeId != "roomieActivity":
            return
        turn_on = action.deviceAction == indigo.kDeviceAction.TurnOn
        if action.deviceAction == indigo.kDeviceAction.Toggle:
            turn_on = not dev.states.get("onOffState", False)
        activity_uuid = dev.pluginProps.get("activityUuid", "")
        activity_name = dev.pluginProps.get("activityName") or activity_uuid
        room_uuid = dev.pluginProps.get("roomUuid", "")
        try:
            if turn_on:
                self._client().run_activity(activity_uuid)
                self.logger.info(f"{dev.name}: started activity '{activity_name}'")
            else:
                self._client().press("ActivityOff", room_uuid)
                self.logger.info(f"{dev.name}: powered off room")
            self._schedule_refresh()
        except RoomieError as exc:
            self.logger.error(
                f"{dev.name}: {'start' if turn_on else 'power off'} failed: {exc}"
            )

    ########################################
    # MCP tool provider (Indigo MCP Server plugin)
    ########################################

    def handle_mcp_tool_invoke(self, action, dev=None, caller_waiting_for_result=True):
        """
        Called cross-plugin by the Indigo MCP Server (com.vtmikel.mcp_server)
        via executeAction. The bare tool name and a JSON-string arguments
        payload arrive in action.props; the reply is always a JSON-string
        envelope (see mcp_api.tool_dispatcher). Everything under mcp_api/ is
        imported lazily here so a bug in tool code can never affect plugin
        startup for users without the MCP Server.
        """
        try:
            if self._tool_dispatcher is None:
                from mcp_api.tool_dispatcher import ToolDispatcher
                from mcp_api.tool_service import RoomieToolService

                self._tool_dispatcher = ToolDispatcher(
                    RoomieToolService(
                        poller=self._poller,
                        client_factory=self._client,
                        bridge=self._indigo_bridge(),
                        plugin_version=self.pluginVersion,
                        prefs=self.pluginPrefs,
                    )
                )
        except Exception as e:
            self.logger.exception("Failed to initialize the MCP tool dispatcher")
            return json.dumps(
                {
                    "status": "error",
                    "error": {
                        "type": "internal",
                        "message": f"MCP tool dispatcher failed to initialize: {e}",
                    },
                }
            )
        tool = action.props.get("tool", "")
        arguments = action.props.get("arguments", "{}")
        return self._tool_dispatcher.dispatch(tool, arguments)

    def _indigo_bridge(self):
        """The Indigo-side lookups the (indigo-free) tool service needs."""
        from mcp_api.tool_service import IndigoBridge

        def room_device_ids(room_uuid):
            with self._dev_lock:
                return sorted(self._devices_by_room.get(room_uuid, ()))

        def activity_devices(room_uuid):
            with self._dev_lock:
                dev_ids = sorted(self._activity_devices_by_room.get(room_uuid, ()))
            entries = []
            for dev_id in dev_ids:
                try:
                    dev = indigo.devices[dev_id]
                except KeyError:
                    continue
                entries.append(
                    {
                        "id": dev_id,
                        "name": dev.name,
                        "activity_uuid": dev.pluginProps.get("activityUuid", ""),
                        "is_on": bool(dev.states.get("onOffState", False)),
                    }
                )
            return entries

        def room_uuid_for_device(dev_id):
            with self._dev_lock:
                for room_uuid, dev_ids in self._devices_by_room.items():
                    if dev_id in dev_ids:
                        return room_uuid
            try:
                dev = indigo.devices[dev_id]
            except KeyError:
                return None
            if dev.deviceTypeId != "roomieRoom":
                return None
            return dev.pluginProps.get("roomUuid") or None

        def device_counts():
            with self._dev_lock:
                rooms = sum(len(ids) for ids in self._devices_by_room.values())
                activities = sum(
                    len(ids) for ids in self._activity_devices_by_room.values()
                )
            return rooms, activities

        return IndigoBridge(
            room_device_ids=room_device_ids,
            activity_devices=activity_devices,
            room_uuid_for_device=room_uuid_for_device,
            schedule_refresh=self._schedule_refresh,
            device_counts=device_counts,
        )

    ########################################
    # Menu items
    ########################################

    def menu_test_connection(self):
        try:
            rooms = self._client().get_rooms()
            names = ", ".join(r.get("roomname", "?") for r in rooms)
            self.logger.info(f"connection OK — {len(rooms)} room(s): {names}")
        except RoomieError as exc:
            self.logger.error(f"connection test failed: {exc}")

    def menu_dump_rooms_activities(self):
        if not self._poller:
            return
        try:
            self._poller.refresh_cache_now()
        except RoomieError as exc:
            self.logger.warning(f"live refresh failed ({exc}); dumping cached data")
        off_uuids = self._poller.off_activity_uuids()
        lines = ["Roomie rooms & activities:"]
        for room in sorted(self._poller.rooms_snapshot(), key=lambda r: r.name.lower()):
            status = room.current_activity_name or "Off"
            lines.append(f"  Room '{room.name}' [{room.uuid}] — current: {status}")
            for activity in room.activities:
                lines.append(f"    - '{activity.name}' [{activity.uuid}]")
            for activity in self._poller.activities_for_room(room.uuid):
                flags = []
                if activity.toggle_state:
                    flags.append(f"toggle:{activity.toggle_state}")
                if activity.uuid in off_uuids:
                    flags.append("off-type")
                if flags:
                    lines.append(
                        f"    - '{activity.name}' [{activity.uuid}] ({', '.join(flags)})"
                    )
        self.logger.info("\n".join(lines))

    def menu_create_room_devices(self):
        if self._poller:
            try:
                self._poller.refresh_cache_now()
            except RoomieError as exc:
                self.logger.warning(f"live refresh failed ({exc}); using cached rooms")
        rooms = self._poller.rooms_snapshot() if self._poller else []
        if not rooms:
            self.logger.warning(
                "no rooms cached; check the connection (Plugins → Roomie Remote → Test Connection)"
            )
            return
        created = self._create_missing_room_devices(rooms)
        if created:
            self.logger.info(
                f"created {len(created)} room device(s): {', '.join(created)}"
            )
        else:
            self.logger.info("all rooms already have Indigo devices")

    def menu_force_refresh_all(self):
        self._force_full = True
        self._poller.request_refresh()
        self.logger.info("full state refresh requested")

    def menu_toggle_debug(self):
        if self.indigo_log_handler.level == logging.DEBUG:
            self.indigo_log_handler.setLevel(self.log_level)
            self.logger.info("debug logging disabled")
        else:
            self.indigo_log_handler.setLevel(logging.DEBUG)
            self.logger.info("debug logging enabled")

    ########################################
    # Poll outcome application
    ########################################

    def _apply_outcome(self, outcome):
        with self._dev_lock:
            devices_by_room = {k: set(v) for k, v in self._devices_by_room.items()}
            activity_devices_by_room = {
                k: set(v) for k, v in self._activity_devices_by_room.items()
            }

        if not outcome.ok:
            if outcome.reachability_changed:
                self.logger.warning(
                    f"Roomie controller unreachable: {outcome.error} — backing off"
                )
                for dev_ids in devices_by_room.values():
                    for dev_id in dev_ids:
                        dev = indigo.devices[dev_id]
                        dev.updateStatesOnServer(
                            [{"key": "controllerReachable", "value": False}]
                        )
                        dev.setErrorStateOnServer("unreachable")
                for dev_ids in activity_devices_by_room.values():
                    for dev_id in dev_ids:
                        indigo.devices[dev_id].setErrorStateOnServer("unreachable")
            return

        if outcome.reachability_changed:
            self.logger.info("Roomie controller reachable again")
        if outcome.activities_error:
            self.logger.debug(f"activities refresh failed: {outcome.activities_error}")

        self._auto_create_new_room_devices()
        self._repair_orphan_devices()

        for room_uuid, changes in outcome.room_state_changes.items():
            for dev_id in devices_by_room.get(room_uuid, ()):
                dev = indigo.devices[dev_id]
                self._push_states(dev, changes)
                if outcome.reachability_changed or getattr(dev, "errorState", None):
                    dev.setErrorStateOnServer(None)
            if "currentActivityUuid" in changes or outcome.reachability_changed:
                current_uuid = changes.get("currentActivityUuid")
                for dev_id in activity_devices_by_room.get(room_uuid, ()):
                    dev = indigo.devices[dev_id]
                    if current_uuid is None:
                        # Activity unchanged this poll; only clearing errors.
                        dev.setErrorStateOnServer(None)
                        continue
                    self._set_activity_device_state(
                        dev, dev.pluginProps.get("activityUuid") == current_uuid
                    )
                    if outcome.reachability_changed or getattr(dev, "errorState", None):
                        dev.setErrorStateOnServer(None)

        for registry in (devices_by_room, activity_devices_by_room):
            for room_uuid, dev_ids in registry.items():
                if room_uuid not in outcome.rooms_present:
                    for dev_id in dev_ids:
                        dev = indigo.devices[dev_id]
                        if getattr(dev, "errorState", None) != "room not found":
                            self.logger.warning(
                                f"{dev.name}: room {room_uuid} no longer exists in Roomie"
                            )
                        dev.setErrorStateOnServer("room not found")

    def _push_states(self, dev, changes):
        update_list = []
        for key, value in changes.items():
            item = {"key": key, "value": value}
            if key == "currentActivityName" and value == "":
                item["uiValue"] = "Off"
            update_list.append(item)
        if update_list:
            dev.updateStatesOnServer(update_list)
        if "onOffState" in changes:
            dev.updateStateImageOnServer(
                indigo.kStateImageSel.PowerOn
                if changes["onOffState"]
                else indigo.kStateImageSel.PowerOff
            )

    def _push_full_state(self, dev, room_uuid):
        for room in self._poller.rooms_snapshot() if self._poller else []:
            if room.uuid == room_uuid:
                self._push_states(dev, RoomiePoller._room_states(room))
                return

    def _set_activity_device_state(self, dev, is_on):
        if dev.states.get("onOffState") == is_on:
            return
        dev.updateStateOnServer("onOffState", is_on)
        dev.updateStateImageOnServer(
            indigo.kStateImageSel.PowerOn if is_on else indigo.kStateImageSel.PowerOff
        )

    def _push_activity_state_from_cache(self, dev, room_uuid):
        for room in self._poller.rooms_snapshot() if self._poller else []:
            if room.uuid == room_uuid:
                self._set_activity_device_state(
                    dev,
                    room.current_activity_uuid == dev.pluginProps.get("activityUuid"),
                )
                return

    ########################################
    # Device auto-creation
    ########################################

    def _auto_create_new_room_devices(self):
        """Create devices for rooms/activities never seen before.

        The "seen" sets only grow while the corresponding auto-create
        checkbox is enabled, so enabling it later still back-fills, and
        devices the user deletes are never re-created (the menu item does
        that explicitly).
        """
        rooms = self._poller.rooms_snapshot()
        prefs_dirty = False

        if bool(self.pluginPrefs.get("autoCreateDevices", True)):
            known = set(json.loads(self.pluginPrefs.get("knownRoomUuids", "[]")))
            new_rooms = [room for room in rooms if room.uuid not in known]
            if new_rooms:
                created = self._create_missing_room_devices(new_rooms)
                if created:
                    self.logger.info(
                        f"auto-created {len(created)} room device(s): "
                        f"{', '.join(created)}"
                    )
                known.update(room.uuid for room in new_rooms)
                self.pluginPrefs["knownRoomUuids"] = json.dumps(sorted(known))
                prefs_dirty = True

        if bool(self.pluginPrefs.get("autoCreateActivityDevices", False)):
            known = set(json.loads(self.pluginPrefs.get("knownActivityUuids", "[]")))
            new_pairs = [
                (room, activity)
                for room in rooms
                for activity in room.activities
                if activity.uuid not in known
            ]
            if new_pairs:
                created = self._create_missing_activity_devices(new_pairs)
                if created:
                    self.logger.info(
                        f"auto-created {len(created)} activity device(s): "
                        f"{', '.join(created)}"
                    )
                known.update(activity.uuid for _, activity in new_pairs)
                self.pluginPrefs["knownActivityUuids"] = json.dumps(sorted(known))
                prefs_dirty = True

        if prefs_dirty:
            save_prefs = getattr(self, "savePluginPrefs", None)
            if save_prefs:
                save_prefs()

    def _create_missing_room_devices(self, rooms):
        existing = {
            dev.pluginProps.get("roomUuid")
            for dev in indigo.devices.iter("self")
            if dev.deviceTypeId == "roomieRoom"
        }
        created = []
        for room in rooms:
            if room.uuid in existing:
                continue
            base_name = DEVICE_NAME_TEMPLATE.format(room=room.name)
            name = base_name
            suffix = 1
            while name in indigo.devices:
                suffix += 1
                name = f"{base_name} {suffix}"
            try:
                indigo.device.create(
                    indigo.kProtocol.Plugin,
                    name=name,
                    deviceTypeId="roomieRoom",
                    props={"roomUuid": room.uuid, "roomName": room.name},
                    folder=self._device_folder_id(),
                )
                created.append(name)
            except Exception as exc:
                self.logger.error(
                    f"failed to create device for room '{room.name}': {exc}"
                )
        return created

    def _create_missing_activity_devices(self, pairs):
        """Create roomieActivity devices for (room, activity) pairs that
        don't already have one (matched on activityUuid)."""
        existing = {
            dev.pluginProps.get("activityUuid")
            for dev in indigo.devices.iter("self")
            if dev.deviceTypeId == "roomieActivity"
        }
        created = []
        for room, activity in pairs:
            if activity.uuid in existing:
                continue
            base_name = ACTIVITY_DEVICE_NAME_TEMPLATE.format(
                room=room.name, activity=activity.name
            )
            name = base_name
            suffix = 1
            while name in indigo.devices:
                suffix += 1
                name = f"{base_name} {suffix}"
            try:
                indigo.device.create(
                    indigo.kProtocol.Plugin,
                    name=name,
                    deviceTypeId="roomieActivity",
                    props={
                        "roomUuid": room.uuid,
                        "roomName": room.name,
                        "activityUuid": activity.uuid,
                        "activityName": activity.name,
                    },
                    folder=self._device_folder_id(),
                )
                created.append(name)
            except Exception as exc:
                self.logger.error(
                    f"failed to create device for activity '{activity.name}': {exc}"
                )
        return created

    def _repair_orphan_devices(self):
        """Re-link roomieRoom devices whose roomUuid prop is missing.

        pluginProps can be silently wiped from outside the plugin (e.g. a
        dev.replaceOnServer() from a script — scripts see plugin props as
        empty and write that back). Only the owning plugin may restore
        them, so on each successful poll we rebuild the link from the
        device's roomName state.
        """
        with self._dev_lock:
            registered = {
                dev_id for ids in self._devices_by_room.values() for dev_id in ids
            }
        rooms_by_name = {r.name: r for r in self._poller.rooms_snapshot()}
        for dev in indigo.devices.iter("self"):
            if dev.deviceTypeId != "roomieRoom" or dev.id in registered:
                continue
            if dev.pluginProps.get("roomUuid"):
                continue  # startComm will pick it up; nothing to repair
            room = rooms_by_name.get(dev.states.get("roomName") or "")
            if room is None:
                # Fallback: match the room name embedded in the device name.
                for room_name, candidate in rooms_by_name.items():
                    if dev.name.endswith(room_name):
                        room = candidate
                        break
            if room is None:
                self.logger.warning(
                    f"'{dev.name}' has no room link and no matching Roomie room; "
                    "re-select the room in the device settings"
                )
                continue
            props = indigo.Dict()
            props["roomUuid"] = room.uuid
            props["roomName"] = room.name
            dev.replacePluginPropsOnServer(props)
            with self._dev_lock:
                self._devices_by_room.setdefault(room.uuid, set()).add(dev.id)
            self._push_states(dev, RoomiePoller._room_states(room))
            dev.setErrorStateOnServer(None)
            self.logger.warning(
                f"repaired missing room link on '{dev.name}' → {room.name} ({room.uuid})"
            )

    def _device_folder_id(self):
        for folder in indigo.devices.folders:
            if folder.name == DEVICE_FOLDER_NAME:
                return folder.id
        try:
            return indigo.devices.folder.create(DEVICE_FOLDER_NAME).id
        except Exception as exc:
            self.logger.debug(f"could not create device folder: {exc}")
            return 0

    ########################################
    # Helpers
    ########################################

    def _build_client_from_prefs(self, prefs=None):
        prefs = prefs if prefs is not None else self.pluginPrefs
        return RoomieClient(
            prefs.get("host", "").strip(),
            port=int(prefs.get("port") or 47147),
            timeout=float(prefs.get("requestTimeout") or 5),
        )

    def _client(self):
        return self._build_client_from_prefs()

    def _schedule_refresh(self, delay=1.5):
        """Poll shortly after an action so Indigo state converges quickly."""
        timer = threading.Timer(delay, self._poller.request_refresh)
        timer.daemon = True
        timer.start()

    def _activity_name(self, uuid):
        if not self._poller:
            return None
        for room in self._poller.rooms_snapshot():
            for activity in room.activities:
                if activity.uuid == uuid:
                    return activity.name
        for room in self._poller.rooms_snapshot():
            for activity in self._poller.activities_for_room(room.uuid):
                if activity.uuid == uuid:
                    return activity.name
        return None

    @staticmethod
    def _validate_int(value, minimum, maximum):
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return number if minimum <= number <= maximum else None

    @staticmethod
    def _validate_float(value, minimum, maximum):
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return None
        return number if minimum <= number <= maximum else None
