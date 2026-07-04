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
from roomie.poller import RoomiePoller

DEVICE_FOLDER_NAME = "Roomie Remote"

# Symbolic buttons from Roomie's Universal Remote API, grouped for the
# Press Remote Button menu. Roomie resolves them against the room's
# current activity; unsupported buttons return 404/422 at press time.
BUTTON_GROUPS = [
    ("Power", ["Power", "PowerOn", "PowerOff"]),
    (
        "Activity",
        ["ActivityOff"] + [f"Activity{n}" for n in range(1, 9)],
    ),
    ("Volume", ["VolumeUp", "VolumeDown", "Mute"]),
    ("Channel", ["ChannelUp", "ChannelDown", "Channel"]),
    (
        "Transport",
        [
            "Play",
            "Pause",
            "Stop",
            "Rewind",
            "FastForward",
            "SkipBack",
            "SkipForward",
            "Record",
        ],
    ),
    (
        "Cursor / Navigation",
        [
            "Up",
            "Down",
            "Left",
            "Right",
            "Select",
            "Back",
            "Exit",
            "Home",
            "Menu",
            "Info",
            "Guide",
            "PageUp",
            "PageDown",
        ],
    ),
    ("Numeric", [str(n) for n in range(10)] + ["Dot", "Enter"]),
    ("Color", ["Red", "Green", "Yellow", "Blue"]),
    ("Extended", ["Input", "Eject", "Search", "Settings"]),
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
        self._devices_by_room = {}  # room_uuid -> set of device ids
        self._force_full = False

    ########################################
    # Lifecycle
    ########################################

    def startup(self):
        self.logger.debug("startup called")
        self._poller = RoomiePoller(
            self._build_client_from_prefs(),
            poll_interval=float(self.pluginPrefs.get("pollInterval", 5)),
        )

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
        with self._dev_lock:
            self._devices_by_room.setdefault(room_uuid, set()).add(dev.id)
        self._push_full_state(dev, room_uuid)
        if self._poller:
            self._poller.request_refresh()

    def deviceStopComm(self, dev):
        self.logger.debug(f"deviceStopComm: {dev.name}")
        room_uuid = dev.pluginProps.get("roomUuid")
        with self._dev_lock:
            if room_uuid in self._devices_by_room:
                self._devices_by_room[room_uuid].discard(dev.id)
                if not self._devices_by_room[room_uuid]:
                    del self._devices_by_room[room_uuid]

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
        for room in self._poller.rooms_snapshot() if self._poller else []:
            if room.uuid == room_uuid:
                values_dict["roomName"] = room.name
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
            if values_dict.get("button", SEPARATOR_VALUE) == SEPARATOR_VALUE:
                errors["button"] = "Choose a button (separators are not buttons)."
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
        if not self._poller or not target_id:
            return [("", "-- open this dialog from a Roomie Room device --")]
        try:
            room_uuid = indigo.devices[target_id].pluginProps.get("roomUuid")
        except KeyError:
            return [("", "-- device not found --")]
        entries = []
        for room in self._poller.rooms_snapshot():
            if room.uuid == room_uuid:
                entries = [(a.uuid, a.name) for a in room.activities]
                break
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
        button = action.props.get("button", "")
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
            return

        if outcome.reachability_changed:
            self.logger.info("Roomie controller reachable again")
        if outcome.activities_error:
            self.logger.debug(f"activities refresh failed: {outcome.activities_error}")

        self._auto_create_new_room_devices()

        for room_uuid, changes in outcome.room_state_changes.items():
            for dev_id in devices_by_room.get(room_uuid, ()):
                dev = indigo.devices[dev_id]
                self._push_states(dev, changes)
                if outcome.reachability_changed or getattr(dev, "errorState", None):
                    dev.setErrorStateOnServer(None)

        for room_uuid, dev_ids in devices_by_room.items():
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

    ########################################
    # Device auto-creation
    ########################################

    def _auto_create_new_room_devices(self):
        rooms = self._poller.rooms_snapshot()
        known = set(json.loads(self.pluginPrefs.get("knownRoomUuids", "[]")))
        new_rooms = [room for room in rooms if room.uuid not in known]
        if not new_rooms:
            return
        if bool(self.pluginPrefs.get("autoCreateDevices", True)):
            created = self._create_missing_room_devices(new_rooms)
            if created:
                self.logger.info(
                    f"auto-created {len(created)} room device(s): {', '.join(created)}"
                )
        known.update(room.uuid for room in rooms)
        self.pluginPrefs["knownRoomUuids"] = json.dumps(sorted(known))
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
            name = room.name
            suffix = 1
            while name in indigo.devices:
                suffix += 1
                name = f"{room.name} {suffix}"
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
