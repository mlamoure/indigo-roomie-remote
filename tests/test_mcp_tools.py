"""Tests for the MCP tool provider: resolvers, the tool service, and the
dispatcher envelope, driven exactly as plugin.handle_mcp_tool_invoke does
(tool name + JSON-string arguments in, JSON-string envelope out)."""

import json
from types import SimpleNamespace

import pytest

from mcp_api.tool_dispatcher import ToolDispatcher
from mcp_api.tool_service import IndigoBridge, RoomieToolService
from roomie.client import RoomieRequestFailed, RoomieUnreachable
from roomie.lexicon import BUTTON_NAMES
from roomie.poller import RoomiePoller

LIVING = {
    "roomuuid": "R1",
    "roomname": "Living Room",
    "currentactivityuuid": "A2",
    "currentactivityname": "Watch Netflix",
    "activities": [
        {"activityuuid": "A1", "name": "Watch Plex", "ic": "logo-plex"},
        {"activityuuid": "A2", "name": "Watch Netflix", "ic": "logo-netflix"},
        {"activityuuid": "A3", "name": "Watch AppleTV", "ic": "logo-appletv"},
        {"activityuuid": "A4", "name": "Watch House AppleTV", "ic": "logo-appletv"},
    ],
}
BASEMENT = {
    "roomuuid": "R2",
    "roomname": "Basement Bar & TV Area",
    "currentactivityuuid": "OFF2",
    "currentactivityname": "System Off",
    "currentactivityoff": True,
    "activities": [
        {"activityuuid": "B1", "name": "Use Basement AppleTV", "ic": "logo-appletv"},
        {"activityuuid": "B2", "name": "Watch Plex", "ic": "logo-plex"},
    ],
}
ACTIVITIES = [
    {
        "uuid": "OFF1",
        "name": "Living Room: System Off",
        "type": "off",
        "roomuuid": "R1",
    },
    {
        "uuid": "OFF2",
        "name": "Basement Bar & TV Area: System Off",
        "type": "off",
        "roomuuid": "R2",
    },
    {
        "uuid": "T1+",
        "name": "Living Room: Shades (On)",
        "roomuuid": "R1",
        "toggle": True,
    },
    {
        "uuid": "T1-",
        "name": "Living Room: Shades (Off)",
        "roomuuid": "R1",
        "toggle": True,
    },
]
DEVICES = [
    {
        "uuid": "D1",
        "name": "Onkyo Receiver",
        "brand": "Onkyo",
        "model": "TX",
        "type": "Rcvr",
        "roomname": "Living Room",
        "roomuuid": "R1",
        "address": "10.0.0.51",
        "port": 60128,
    },
    {
        "uuid": "D2",
        "name": "Apple Media Player",
        "brand": "Apple",
        "model": "Apple TV",
        "type": "Media",
        "roomname": "Living Room",
        "roomuuid": "R1",
        "address": "10.0.0.59",
        "port": 7000,
    },
    {
        "uuid": "D3",
        "name": "Living Room Lamp",
        "brand": "Aeon",
        "model": "Switch",
        "type": "Switch",
        "roomname": "Living Room",
        "roomuuid": "R1",
        "address": "",
        "port": "",
    },
    {
        "uuid": "D4",
        "name": "Denon Receiver",
        "brand": "Denon",
        "model": "AVR",
        "type": "Rcvr",
        "roomname": "Basement Bar & TV Area",
        "roomuuid": "R2",
        "address": "10.0.0.52",
        "port": 23,
    },
]
CAPABILITIES = {
    "roomuuid": "R1",
    "roomname": "Living Room",
    "activityuuid": "A2",
    "activityname": "Watch Netflix",
    "lexicon_version": 1,
    "buttons": {
        "VolumeUp": {
            "command": "VOLUME UP",
            "deviceuuid": "D1",
            "button": "VolumeUp",
            "role": "volume",
            "category": "volume",
        },
        "Play": {
            "command": "PLAY",
            "deviceuuid": "D2",
            "button": "Play",
            "role": "transport",
            "category": "transport",
        },
        "Activity1": {
            "activityuuid": "A1",
            "activityname": "Watch Plex",
            "button": "Activity1",
            "role": "activity",
            "category": "activity",
        },
        "ChannelUp": None,
        "Red": None,
    },
}


def refused(code):
    return RoomieRequestFailed(f"HTTP {code}", co=code, status_code=code)


class FakeClient:
    """Scriptable RoomieClient stand-in recording every call."""

    def __init__(self):
        self.rooms = [LIVING, BASEMENT]
        self.activities = list(ACTIVITIES)
        self.devices = list(DEVICES)
        self.capabilities = CAPABILITIES
        self.calls = []
        self.fail_rooms = False
        self.fail_next = None  # exception raised by the next non-poll call

    def _maybe_fail(self):
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    def get_rooms(self):
        if self.fail_rooms:
            raise RoomieUnreachable("connection refused")
        return self.rooms

    def get_activities(self):
        return self.activities

    def get_devices(self):
        self.calls.append(("get_devices",))
        self._maybe_fail()
        return self.devices

    def get_capabilities(self, roomuuid=None, activityuuid=None, roomname=None):
        self.calls.append(("get_capabilities", roomuuid, activityuuid))
        self._maybe_fail()
        return self.capabilities

    def run_activity(self, uuid, ts=None, delay=None):
        self.calls.append(("run_activity", uuid, ts, delay))
        self._maybe_fail()
        return {}

    def press(
        self,
        button,
        roomuuid,
        count=1,
        digits=None,
        action=None,
        hold_ms=None,
        activityuuid=None,
    ):
        self.calls.append(
            ("press", button, roomuuid, count, digits, action, hold_ms, activityuuid)
        )
        self._maybe_fail()
        reply = {
            "button": button,
            "role": "volume",
            "deviceuuid": "D1",
            "command": "X",
            "dispatched": True,
        }
        if action == "press":
            reply["session"] = "S1"
        return reply


def build(client=None, poll=True, prefs=None):
    client = client or FakeClient()
    poller = RoomiePoller(client, poll_interval=5.0)
    if poll:
        poller.poll_once()
    refreshes = []
    bridge = IndigoBridge(
        room_device_ids=lambda uuid: {"R1": [1566], "R2": [7926]}.get(uuid, []),
        activity_devices=lambda uuid: (
            [{"id": 42, "name": "LR Netflix", "activity_uuid": "A2", "is_on": True}]
            if uuid == "R1"
            else []
        ),
        room_uuid_for_device=lambda dev_id: {1566: "R1", 7926: "R2"}.get(dev_id),
        schedule_refresh=lambda: refreshes.append(True),
        device_counts=lambda: (2, 1),
    )
    service = RoomieToolService(
        poller,
        lambda: client,
        bridge,
        "2026.9.0",
        prefs
        or {
            "host": "roomie.local",
            "port": "47147",
            "pollInterval": "5",
            "requestTimeout": "5",
        },
    )
    return SimpleNamespace(
        dispatcher=ToolDispatcher(service),
        client=client,
        poller=poller,
        refreshes=refreshes,
    )


@pytest.fixture
def env():
    return build()


def call(env, tool, **arguments):
    reply = json.loads(env.dispatcher.dispatch(tool, json.dumps(arguments)))
    assert reply["status"] in ("ok", "error")
    return reply


def ok(env, tool, **arguments):
    reply = call(env, tool, **arguments)
    assert reply["status"] == "ok", reply
    return reply["result"]


def err(env, tool, **arguments):
    reply = call(env, tool, **arguments)
    assert reply["status"] == "error", reply
    return reply["error"]


# ----------------------------------------------------------------------
# dispatcher envelope


class TestDispatcher:
    def test_unknown_tool(self, env):
        error = err(env, "explode")
        assert error["type"] == "not_found"
        assert "list_rooms" in error["message"]

    def test_invalid_json_arguments(self, env):
        reply = json.loads(env.dispatcher.dispatch("list_rooms", "{not json"))
        assert reply["error"]["type"] == "validation"

    def test_unexpected_argument(self, env):
        assert err(env, "list_rooms", bogus=1)["type"] == "validation"

    def test_missing_required_argument(self, env):
        assert err(env, "get_room")["type"] == "validation"


# ----------------------------------------------------------------------
# list_rooms / get_room


class TestListRooms:
    def test_rooms_sorted_with_state_and_indigo_ids(self, env):
        result = ok(env, "list_rooms")
        names = [r["name"] for r in result["rooms"]]
        assert names == ["Basement Bar & TV Area", "Living Room"]
        living = result["rooms"][1]
        assert living["is_on"] is True
        assert living["current_activity"] == {"uuid": "A2", "name": "Watch Netflix"}
        assert living["activity_count"] == 4
        assert living["indigo_device_ids"] == [1566]
        basement = result["rooms"][0]
        assert basement["is_on"] is False
        assert basement["current_activity"] is None
        assert result["controller"]["reachable"] is True
        assert result["controller"]["last_poll"]

    def test_refresh_fetches_live_and_wakes_poller(self, env):
        env.client.rooms = [dict(LIVING, roomname="Great Room")]
        result = ok(env, "list_rooms", refresh=True)
        assert [r["name"] for r in result["rooms"]] == [
            "Basement Bar & TV Area",
            "Great Room",
        ][1:]
        assert env.poller.wake_event.is_set()

    def test_refresh_must_be_boolean(self, env):
        assert err(env, "list_rooms", refresh="yes")["type"] == "validation"

    def test_empty_cache_triggers_one_live_fetch(self):
        env = build(poll=False)
        result = ok(env, "list_rooms")
        assert len(result["rooms"]) == 2

    def test_empty_cache_and_unreachable_is_internal_with_hint(self):
        client = FakeClient()
        client.fail_rooms = True
        env = build(client, poll=False)
        error = err(env, "list_rooms")
        assert error["type"] == "internal"
        assert "Local Network Control" in error["message"]


class TestGetRoom:
    @pytest.mark.parametrize(
        "ref", ["Living Room", "living room", "R1", "1566", "living"]
    )
    def test_addressing_forms(self, env, ref):
        assert ok(env, "get_room", room=ref)["room_uuid"] == "R1"

    def test_unique_substring(self, env):
        assert ok(env, "get_room", room="basement")["room_uuid"] == "R2"

    def test_ambiguous_substring_lists_matches(self, env):
        error = err(env, "get_room", room="r")
        assert error["type"] == "validation"
        assert len(error["details"]["matches"]) == 2

    def test_unknown_room_lists_candidates(self, env):
        error = err(env, "get_room", room="Attic")
        assert error["type"] == "not_found"
        assert set(error["details"]["candidates"]) == {
            "Living Room",
            "Basement Bar & TV Area",
        }

    def test_unknown_device_id_falls_through_to_not_found(self, env):
        error = err(env, "get_room", room="999")
        assert error["type"] == "not_found"
        assert "Indigo id" in error["message"]

    def test_detail_payload(self, env):
        result = ok(env, "get_room", room="R1")
        assert [a["is_current"] for a in result["activities"]] == [
            False,
            True,
            False,
            False,
        ]
        assert result["activities"][0]["icon"] == "logo-plex"
        assert [t["uuid"] for t in result["toggle_activities"]] == ["T1+", "T1-"]
        assert result["toggle_activities"][0]["toggle_state"] == "on"
        assert result["off_activities"] == [
            {"uuid": "OFF1", "name": "Living Room: System Off"}
        ]
        assert result["indigo_activity_devices"][0]["name"] == "LR Netflix"
        assert result["indigo_device_ids"] == [1566]
        assert result["last_poll"]


# ----------------------------------------------------------------------
# get_capabilities / list_devices / get_status


class TestGetCapabilities:
    def test_grouped_by_category_with_device_names(self, env):
        result = ok(env, "get_capabilities", room="living room")
        assert env.client.calls[0] == ("get_capabilities", "R1", None)
        assert result["activity_name"] == "Watch Netflix"
        assert result["supported"]["volume"] == [
            {
                "button": "VolumeUp",
                "role": "volume",
                "command": "VOLUME UP",
                "device_uuid": "D1",
                "device_name": "Onkyo Receiver",
            }
        ]
        assert result["supported"]["activity"][0]["activity_name"] == "Watch Plex"
        assert result["unsupported"] == ["ChannelUp", "Red"]
        assert "note" not in result

    def test_room_off_is_conflict_pointing_at_start_activity(self, env):
        env.client.fail_next = refused(409)
        error = err(env, "get_capabilities", room="basement")
        assert error["type"] == "conflict"
        assert "roomie_start_activity" in error["message"]

    def test_activity_override_resolves_within_room(self, env):
        ok(env, "get_capabilities", room="R1", activity="plex")
        assert env.client.calls[0] == ("get_capabilities", None, "A1")

    def test_newer_lexicon_is_flagged(self, env):
        env.client.capabilities = dict(CAPABILITIES, lexicon_version=2)
        assert "lexicon_version 2" in ok(env, "get_capabilities", room="R1")["note"]

    def test_device_name_lookup_failure_is_tolerated(self, env):
        env.client.devices = None
        assert (
            ok(env, "get_capabilities", room="R1")["supported"]["volume"][0][
                "device_uuid"
            ]
            == "D1"
        )


class TestListDevices:
    def test_default_is_network_devices_only(self, env):
        result = ok(env, "list_devices")
        assert [d["name"] for d in result["devices"]] == [
            "Onkyo Receiver",
            "Apple Media Player",
            "Denon Receiver",
        ]
        assert result["count"] == 3
        assert result["total_in_roomie"] == 4
        assert result["filter"] == "network_devices_only"

    def test_include_all_and_room_filter(self, env):
        result = ok(env, "list_devices", room="living", include_all=True)
        assert [d["uuid"] for d in result["devices"]] == ["D1", "D2", "D3"]
        assert result["devices"][2]["address"] is None

    def test_unreachable_is_internal(self, env):
        env.client.fail_next = RoomieUnreachable("timeout")
        assert err(env, "list_devices")["type"] == "internal"


class TestGetStatus:
    def test_fields(self, env):
        result = ok(env, "get_status")
        assert result["plugin_version"] == "2026.9.0"
        assert result["host"] == "roomie.local"
        assert result["port"] == 47147
        assert result["reachable"] is True
        assert result["consecutive_failures"] == 0
        assert result["current_backoff_seconds"] == 5.0
        assert result["rooms_cached"] == 2
        assert result["activities_cached"] == 4
        assert result["indigo_room_devices"] == 2
        assert result["indigo_activity_devices"] == 1
        assert "live_test" not in result

    def test_test_connection_ok(self, env):
        live = ok(env, "get_status", test_connection=True)["live_test"]
        assert live == {
            "ok": True,
            "room_count": 2,
            "rooms": ["Living Room", "Basement Bar & TV Area"],
        }

    def test_unreachable_reported_without_raising(self, env):
        env.client.fail_rooms = True
        env.poller.poll_once()
        result = ok(env, "get_status", test_connection=True)
        assert result["reachable"] is False
        assert result["consecutive_failures"] == 1
        assert result["current_backoff_seconds"] == 10.0
        assert "Local Network Control" in result["hint"]
        assert result["live_test"]["ok"] is False


# ----------------------------------------------------------------------
# start_activity


class TestStartActivity:
    def test_by_name_dispatches_and_schedules_refresh(self, env):
        result = ok(env, "start_activity", room="living room", activity="Watch Plex")
        assert env.client.calls == [("run_activity", "A1", None, None)]
        assert env.refreshes == [True]
        assert result["activity"] == {"uuid": "A1", "name": "Watch Plex"}
        assert result["was_already_running"] is False
        assert result["dispatched"] is True
        assert "roomie_get_room" in result["note"]

    def test_already_running_is_dispatched_and_reported(self, env):
        result = ok(env, "start_activity", room="R1", activity="netflix")
        assert result["was_already_running"] is True
        assert env.client.calls == [("run_activity", "A2", None, None)]

    def test_activity_is_room_scoped(self, env):
        result = ok(env, "start_activity", room="basement", activity="Watch Plex")
        assert result["activity"]["uuid"] == "B2"

    def test_exact_name_beats_substring(self, env):
        assert (
            ok(env, "start_activity", room="R1", activity="watch appletv")["activity"][
                "uuid"
            ]
            == "A3"
        )

    def test_unique_substring(self, env):
        assert (
            ok(env, "start_activity", room="R1", activity="house")["activity"]["uuid"]
            == "A4"
        )

    def test_ambiguous_substring(self, env):
        error = err(env, "start_activity", room="R1", activity="AppleTV")
        assert error["type"] == "validation"
        assert {m["uuid"] for m in error["details"]["matches"]} == {"A3", "A4"}

    def test_unknown_activity_lists_candidates(self, env):
        error = err(env, "start_activity", room="R1", activity="Watch Hulu")
        assert error["type"] == "not_found"
        assert "A1" in {c["uuid"] for c in error["details"]["candidates"]}

    def test_delay_passed_through(self, env):
        result = ok(env, "start_activity", room="R1", activity="A1", delay_seconds=2)
        assert env.client.calls[0] == ("run_activity", "A1", None, 2.0)
        assert result["delay_seconds"] == 2.0

    @pytest.mark.parametrize("delay", [0.5, "soon", True])
    def test_bad_delay_rejected(self, env, delay):
        assert (
            err(env, "start_activity", room="R1", activity="A1", delay_seconds=delay)[
                "type"
            ]
            == "validation"
        )
        assert env.client.calls == []

    def test_toggle_by_state_picks_suffixed_uuid(self, env):
        result = ok(
            env, "start_activity", room="R1", activity="Shades", toggle_state="on"
        )
        assert env.client.calls == [("run_activity", "T1+", None, None)]
        assert result["activity"]["toggle_state"] == "on"

    def test_toggle_by_suffixed_name(self, env):
        ok(env, "start_activity", room="R1", activity="Shades (Off)")
        assert env.client.calls == [("run_activity", "T1-", None, None)]

    def test_toggle_by_base_uuid_and_state(self, env):
        ok(env, "start_activity", room="R1", activity="T1", toggle_state="off")
        assert env.client.calls == [("run_activity", "T1-", None, None)]

    def test_toggle_without_state_is_validation(self, env):
        error = err(env, "start_activity", room="R1", activity="Shades")
        assert error["type"] == "validation"
        assert "toggle_state" in error["message"]

    def test_bad_toggle_state(self, env):
        assert (
            err(
                env,
                "start_activity",
                room="R1",
                activity="Shades",
                toggle_state="maybe",
            )["type"]
            == "validation"
        )

    def test_off_type_activity_is_flagged(self, env):
        result = ok(env, "start_activity", room="R1", activity="System Off")
        assert result["is_off_activity"] is True
        assert env.client.calls == [("run_activity", "OFF1", None, None)]

    def test_roomie_404_is_not_found(self, env):
        env.client.fail_next = refused(404)
        error = err(env, "start_activity", room="R1", activity="A1")
        assert error["type"] == "not_found"
        assert env.refreshes == []

    def test_unreachable_is_internal(self, env):
        env.client.fail_next = RoomieUnreachable("timeout")
        assert (
            err(env, "start_activity", room="R1", activity="A1")["type"] == "internal"
        )


# ----------------------------------------------------------------------
# power_off_room


class TestPowerOffRoom:
    def test_on_room_presses_activity_off(self, env):
        result = ok(env, "power_off_room", room="living")
        assert env.client.calls == [
            ("press", "ActivityOff", "R1", 1, None, None, None, None)
        ]
        assert result == {
            "room": {"uuid": "R1", "name": "Living Room"},
            "dispatched": True,
            "was_already_off": False,
            "note": result["note"],
        }
        assert env.refreshes == [True]

    def test_off_room_is_reported_not_errored(self, env):
        result = ok(env, "power_off_room", room="basement")
        assert result["was_already_off"] is True
        assert result["dispatched"] is True  # live controllers answer success

    def test_roomie_409_means_already_off(self, env):
        env.client.fail_next = refused(409)
        result = ok(env, "power_off_room", room="R1")
        assert result["dispatched"] is False
        assert result["was_already_off"] is True

    def test_other_failures_propagate(self, env):
        env.client.fail_next = refused(500)
        assert err(env, "power_off_room", room="R1")["type"] == "internal"


# ----------------------------------------------------------------------
# press_button


class TestPressButton:
    def test_tap_burst(self, env):
        result = ok(env, "press_button", room="R1", button="VolumeUp", count=3)
        assert env.client.calls == [
            ("press", "VolumeUp", "R1", 3, None, None, None, None)
        ]
        assert result["button"] == "VolumeUp"
        assert result["count"] == 3
        assert result["role"] == "volume"
        assert result["device_uuid"] == "D1"
        assert result["resolved_against"] == "Watch Netflix"
        assert env.refreshes == []  # volume does not change room state

    def test_device_name_from_cache(self, env):
        ok(env, "list_devices")
        assert (
            ok(env, "press_button", room="R1", button="VolumeUp")["device_name"]
            == "Onkyo Receiver"
        )

    def test_alias_normalized(self, env):
        assert (
            ok(env, "press_button", room="R1", button="Mute")["button"] == "VolumeMute"
        )

    def test_unknown_button_lists_lexicon(self, env):
        error = err(env, "press_button", room="R1", button="Bogus")
        assert error["type"] == "validation"
        assert error["details"]["valid_buttons"] == BUTTON_NAMES
        assert env.client.calls == []

    @pytest.mark.parametrize("count", [0, 26, "three", True])
    def test_bad_count(self, env, count):
        assert (
            err(env, "press_button", room="R1", button="VolumeUp", count=count)["type"]
            == "validation"
        )

    def test_channel_digits(self, env):
        result = ok(env, "press_button", room="R1", button="Channel", digits="704")
        assert env.client.calls[0][4] == "704"
        assert result["digits"] == "704"

    def test_digits_require_channel_button(self, env):
        assert (
            err(env, "press_button", room="R1", button="VolumeUp", digits="7")["type"]
            == "validation"
        )

    def test_digits_must_be_numeric(self, env):
        assert (
            err(env, "press_button", room="R1", button="Channel", digits="7a")["type"]
            == "validation"
        )

    def test_hold_sends_press_action(self, env):
        result = ok(env, "press_button", room="R1", button="VolumeUp", hold_ms=1500)
        assert env.client.calls == [
            ("press", "VolumeUp", "R1", 1, None, "press", 1500, None)
        ]
        assert result["hold_ms"] == 1500
        assert result["session"] == "S1"

    def test_hold_bounds_and_exclusivity(self, env):
        assert (
            err(env, "press_button", room="R1", button="VolumeUp", hold_ms=20000)[
                "type"
            ]
            == "validation"
        )
        assert (
            err(
                env, "press_button", room="R1", button="VolumeUp", hold_ms=500, count=2
            )["type"]
            == "validation"
        )

    def test_activity_override(self, env):
        result = ok(env, "press_button", room="R1", button="Play", activity="plex")
        assert env.client.calls[0][7] == "A1"
        assert result["resolved_against"] == "Watch Plex"

    def test_activity_button_schedules_refresh(self, env):
        ok(env, "press_button", room="R1", button="Activity1")
        ok(env, "press_button", room="R1", button="PowerOff")
        assert env.refreshes == [True, True]

    def test_room_off_is_conflict(self, env):
        env.client.fail_next = refused(409)
        error = err(env, "press_button", room="basement", button="Play")
        assert error["type"] == "conflict"
        assert "roomie_start_activity" in error["message"]

    def test_unsupported_button_is_conflict(self, env):
        env.client.fail_next = refused(422)
        error = err(env, "press_button", room="R1", button="ChannelUp")
        assert error["type"] == "conflict"
        assert "roomie_get_capabilities" in error["message"]
        assert "Watch Netflix" in error["message"]

    def test_roomie_400_is_validation(self, env):
        env.client.fail_next = refused(400)
        assert (
            err(env, "press_button", room="R1", button="Play")["type"] == "validation"
        )
