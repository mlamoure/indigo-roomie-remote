"""Plugin-level tests: applying poll outcomes to devices, automatic
device creation, and ConfigUI validation. Uses the conftest indigo stub."""

import json

import indigo
import pytest

from plugin import Plugin
from roomie.poller import RoomiePoller
from tests.test_poller import ROOM_ON, ROOM_OFF, FakeClient


@pytest.fixture
def plugin():
    plugin = Plugin(
        "com.vtmikel.roomie",
        "Roomie Remote",
        "2026.1.0",
        {"host": "10.0.0.5", "autoCreateDevices": True},
    )
    return plugin


def attach_poller(plugin, client):
    plugin._poller = RoomiePoller(client, poll_interval=5.0)
    return plugin._poller


def make_room_device(plugin, room_uuid="R1", name="Living Room"):
    dev = indigo.Device(
        name=name,
        deviceTypeId="roomieRoom",
        pluginProps={"roomUuid": room_uuid, "roomName": name},
    )
    indigo.devices[dev.id] = dev
    plugin.deviceStartComm(dev)
    return dev


class TestApplyOutcome:
    def test_states_pushed_to_device(self, plugin):
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        dev = make_room_device(plugin)
        plugin._apply_outcome(poller.poll_once())
        assert dev.states["onOffState"] is True
        assert dev.states["currentActivityName"] == "Watch Plex"
        assert dev.states["controllerReachable"] is True

    def test_off_room_gets_ui_value_off(self, plugin):
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_OFF]))
        dev = make_room_device(plugin)
        plugin._apply_outcome(poller.poll_once())
        update = [
            item
            for batch in dev.state_updates
            for item in batch
            if item["key"] == "currentActivityName"
        ][-1]
        assert update["value"] == ""
        assert update["uiValue"] == "Off"
        assert dev.stateImage == indigo.kStateImageSel.PowerOff

    def test_on_room_gets_power_on_image(self, plugin):
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        dev = make_room_device(plugin)
        plugin._apply_outcome(poller.poll_once())
        assert dev.stateImage == indigo.kStateImageSel.PowerOn

    def test_unreachable_sets_error_state_once(self, plugin):
        client = FakeClient(rooms=[ROOM_ON])
        poller = attach_poller(plugin, client)
        dev = make_room_device(plugin)
        plugin._apply_outcome(poller.poll_once())
        client.fail_rooms = True
        plugin._apply_outcome(poller.poll_once())
        assert dev.errorState == "unreachable"
        assert dev.states["controllerReachable"] is False
        error_calls = len(dev.error_state_calls)
        plugin._apply_outcome(poller.poll_once())  # still failing: no re-push
        assert len(dev.error_state_calls) == error_calls

    def test_recovery_clears_error_and_repushes(self, plugin):
        client = FakeClient(rooms=[ROOM_ON])
        poller = attach_poller(plugin, client)
        dev = make_room_device(plugin)
        plugin._apply_outcome(poller.poll_once())
        client.fail_rooms = True
        plugin._apply_outcome(poller.poll_once())
        client.fail_rooms = False
        plugin._apply_outcome(poller.poll_once())
        assert dev.errorState is None
        assert dev.states["controllerReachable"] is True
        assert dev.states["onOffState"] is True

    def test_room_disappeared_sets_room_not_found(self, plugin):
        client = FakeClient(rooms=[ROOM_ON])
        poller = attach_poller(plugin, client)
        dev = make_room_device(plugin)
        plugin._apply_outcome(poller.poll_once())
        client.rooms = []
        plugin._apply_outcome(poller.poll_once())
        assert dev.errorState == "room not found"


class TestAutoCreate:
    def test_new_rooms_create_devices_in_folder(self, plugin):
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        assert len(created_devices) == 1
        dev = created_devices[0]
        assert dev.name == "Roomie Remote - Living Room"
        assert dev.pluginProps["roomUuid"] == "R1"
        assert indigo.devices.folders[0].name == "Roomie Remote"
        assert dev.folderId == indigo.devices.folders[0].id

    def test_known_rooms_not_recreated_after_delete(self, plugin):
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        # Mike deletes the auto-created device...
        dev = created_devices[0]
        del indigo.devices[dev.id]
        created_devices.clear()
        # ...and the next poll must NOT re-create it.
        plugin._apply_outcome(poller.poll_once())
        assert created_devices == []
        assert "R1" in json.loads(plugin.pluginPrefs["knownRoomUuids"])

    def test_auto_create_disabled_creates_and_records_nothing(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        assert created_devices == []
        assert json.loads(plugin.pluginPrefs.get("knownRoomUuids", "[]")) == []

    def test_enabling_later_backfills(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        plugin._apply_outcome(poller.poll_once())
        plugin.pluginPrefs["autoCreateDevices"] = True
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        assert [d.name for d in created_devices] == ["Roomie Remote - Living Room"]

    def test_existing_device_not_duplicated(self, plugin):
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        make_room_device(plugin, room_uuid="R1", name="My Living Room")
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        assert created_devices == []

    def test_name_collision_gets_suffix(self, plugin):
        # A non-plugin device already holds the templated name.
        other = indigo.Device(name="Roomie Remote - Living Room", deviceTypeId="relay")
        indigo.devices[other.id] = other
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        assert created_devices[0].name == "Roomie Remote - Living Room 2"

    def test_menu_recreates_unconditionally(self, plugin):
        client = FakeClient(rooms=[ROOM_ON])
        poller = attach_poller(plugin, client)
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        dev = created_devices[0]
        del indigo.devices[dev.id]
        created_devices.clear()
        plugin.menu_create_room_devices()
        assert len(created_devices) == 1


class RecordingClient:
    """Records action calls made through plugin._client()."""

    def __init__(self):
        self.calls = []

    def run_activity(self, uuid, ts=None, delay=None):
        self.calls.append(("run_activity", uuid, ts, delay))

    def press(self, button, roomuuid, count=1, digits=None):
        self.calls.append(("press", button, roomuuid, count, digits))


def make_activity_device(plugin, activity_uuid, name, room_uuid="R1"):
    dev = indigo.Device(
        name=name,
        deviceTypeId="roomieActivity",
        pluginProps={
            "roomUuid": room_uuid,
            "roomName": "Living Room",
            "activityUuid": activity_uuid,
            "activityName": name,
        },
    )
    indigo.devices[dev.id] = dev
    plugin.deviceStartComm(dev)
    return dev


class TestActivityDevices:
    def test_tracks_running_activity(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        client = FakeClient(rooms=[ROOM_ON])  # current activity: A1
        poller = attach_poller(plugin, client)
        dev_a1 = make_activity_device(plugin, "A1", "Watch Plex (LR)")
        dev_a2 = make_activity_device(plugin, "A2", "Watch Netflix (LR)")
        plugin._apply_outcome(poller.poll_once())
        assert dev_a1.states["onOffState"] is True
        assert dev_a2.states["onOffState"] is False

    def test_follows_activity_change(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        client = FakeClient(rooms=[ROOM_ON])
        poller = attach_poller(plugin, client)
        dev_a1 = make_activity_device(plugin, "A1", "Watch Plex (LR)")
        dev_a2 = make_activity_device(plugin, "A2", "Watch Netflix (LR)")
        plugin._apply_outcome(poller.poll_once())
        changed = dict(ROOM_ON)
        changed["currentactivityuuid"] = "A2"
        changed["currentactivityname"] = "Watch Netflix"
        client.rooms = [changed]
        plugin._apply_outcome(poller.poll_once())
        assert dev_a1.states["onOffState"] is False
        assert dev_a2.states["onOffState"] is True
        assert dev_a2.stateImage == indigo.kStateImageSel.PowerOn

    def test_room_off_turns_activity_device_off(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        client = FakeClient(rooms=[ROOM_ON])
        poller = attach_poller(plugin, client)
        dev_a1 = make_activity_device(plugin, "A1", "Watch Plex (LR)")
        plugin._apply_outcome(poller.poll_once())
        client.rooms = [ROOM_OFF]
        plugin._apply_outcome(poller.poll_once())
        assert dev_a1.states["onOffState"] is False

    def test_start_comm_pushes_state_from_cache(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        poller.poll_once()
        dev = make_activity_device(plugin, "A1", "Watch Plex (LR)")
        assert dev.states["onOffState"] is True

    def test_turn_on_runs_activity(self, plugin):
        attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        recorder = RecordingClient()
        plugin._client = lambda: recorder
        dev = make_activity_device(plugin, "A2", "Watch Netflix (LR)")
        action = type("A", (), {"deviceAction": indigo.kDeviceAction.TurnOn})()
        plugin.actionControlDevice(action, dev)
        assert recorder.calls == [("run_activity", "A2", None, None)]

    def test_turn_off_presses_activity_off(self, plugin):
        attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        recorder = RecordingClient()
        plugin._client = lambda: recorder
        dev = make_activity_device(plugin, "A2", "Watch Netflix (LR)")
        action = type("A", (), {"deviceAction": indigo.kDeviceAction.TurnOff})()
        plugin.actionControlDevice(action, dev)
        assert recorder.calls == [("press", "ActivityOff", "R1", 1, None)]

    def test_toggle_uses_current_state(self, plugin):
        attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        recorder = RecordingClient()
        plugin._client = lambda: recorder
        dev = make_activity_device(plugin, "A2", "Watch Netflix (LR)")
        dev.states["onOffState"] = True
        action = type("A", (), {"deviceAction": indigo.kDeviceAction.Toggle})()
        plugin.actionControlDevice(action, dev)
        assert recorder.calls[0][0] == "press"  # was on -> power off

    def test_unreachable_marks_activity_devices(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        client = FakeClient(rooms=[ROOM_ON])
        poller = attach_poller(plugin, client)
        dev = make_activity_device(plugin, "A1", "Watch Plex (LR)")
        plugin._apply_outcome(poller.poll_once())
        client.fail_rooms = True
        plugin._apply_outcome(poller.poll_once())
        assert dev.errorState == "unreachable"
        client.fail_rooms = False
        plugin._apply_outcome(poller.poll_once())
        assert dev.errorState is None

    def test_device_config_requires_activity(self, plugin):
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        poller.poll_once()
        ok, _, errors = plugin.validateDeviceConfigUi(
            {"roomUuid": "R1", "activityUuid": ""}, "roomieActivity", 1
        )
        assert not ok
        assert "activityUuid" in errors

    def test_device_config_stores_names(self, plugin):
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        poller.poll_once()
        ok, values = plugin.validateDeviceConfigUi(
            {"roomUuid": "R1", "activityUuid": "A2"}, "roomieActivity", 1
        )
        assert ok
        assert values["roomName"] == "Living Room"
        assert values["activityName"] == "Watch Netflix"

    def test_auto_create_activity_devices(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        plugin.pluginPrefs["autoCreateActivityDevices"] = True
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        assert [d.name for d in created_devices] == [
            "Roomie Remote - Living Room - Watch Plex",
            "Roomie Remote - Living Room - Watch Netflix",
        ]
        dev = created_devices[0]
        assert dev.deviceTypeId == "roomieActivity"
        assert dev.pluginProps["activityUuid"] == "A1"
        assert dev.pluginProps["roomUuid"] == "R1"
        assert json.loads(plugin.pluginPrefs["knownActivityUuids"]) == ["A1", "A2"]

    def test_auto_create_activity_devices_off_by_default(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        assert created_devices == []

    def test_deleted_activity_device_not_recreated(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        plugin.pluginPrefs["autoCreateActivityDevices"] = True
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        for dev in created_devices:
            del indigo.devices[dev.id]
        created_devices.clear()
        plugin._apply_outcome(poller.poll_once())
        assert created_devices == []

    def test_manually_created_activity_device_not_duplicated(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        plugin.pluginPrefs["autoCreateActivityDevices"] = True
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        make_activity_device(plugin, "A1", "My Plex Tile")
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        assert [d.pluginProps["activityUuid"] for d in created_devices] == ["A2"]

    def test_activity_list_from_values_dict_visible_only(self, plugin):
        toggles = [
            {"uuid": "T1+", "name": "Shades (On)", "roomuuid": "R1"},
            {"uuid": "T1-", "name": "Shades (Off)", "roomuuid": "R1"},
        ]
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON], activities=toggles))
        poller.poll_once()
        entries = plugin.get_activity_list(
            filter="visibleOnly", values_dict={"roomUuid": "R1"}
        )
        values = [value for value, _ in entries]
        assert values == ["A1", "A2"]  # no toggle variants, no separator


class TestRepairOrphanDevices:
    """pluginProps wiped from outside the plugin get rebuilt on poll."""

    def make_orphan(self, name="Roomie Remote - Living Room", room_name="Living Room"):
        dev = indigo.Device(name=name, deviceTypeId="roomieRoom", pluginProps={})
        if room_name is not None:
            dev.states["roomName"] = room_name
        indigo.devices[dev.id] = dev
        return dev

    def test_orphan_repaired_from_room_name_state(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        dev = self.make_orphan()
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        plugin._apply_outcome(poller.poll_once())
        assert dev.pluginProps["roomUuid"] == "R1"
        assert dev.pluginProps["roomName"] == "Living Room"
        assert dev.states["onOffState"] is True
        assert dev.errorState is None
        # registered: next outcome updates it
        outcome = poller.poll_once(force_full=True)
        plugin._apply_outcome(outcome)
        assert dev.states["currentActivityName"] == "Watch Plex"

    def test_orphan_repaired_via_device_name_fallback(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        dev = self.make_orphan(room_name=None)
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        plugin._apply_outcome(poller.poll_once())
        assert dev.pluginProps["roomUuid"] == "R1"

    def test_orphan_without_match_left_alone(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        dev = self.make_orphan(name="Some Random Device", room_name="Gone Room")
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        plugin._apply_outcome(poller.poll_once())
        assert dev.pluginProps == {}

    def test_healthy_devices_untouched(self, plugin):
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        dev = make_room_device(plugin)
        props_before = dict(dev.pluginProps)
        plugin._apply_outcome(poller.poll_once())
        assert dev.pluginProps == props_before


class TestValidation:
    def test_prefs_bad_host_rejected(self, plugin):
        ok, _, errors = plugin.validatePrefsConfigUi(
            {
                "host": "definitely-not-a-real-host.invalid",
                "port": "47147",
                "pollInterval": "5",
                "requestTimeout": "5",
            }
        )
        assert not ok
        assert "host" in errors

    def test_prefs_bounds(self, plugin):
        ok, _, errors = plugin.validatePrefsConfigUi(
            {
                "host": "10.0.0.5",
                "port": "0",
                "pollInterval": "1",
                "requestTimeout": "99",
            }
        )
        assert not ok
        assert {"port", "pollInterval", "requestTimeout"} <= set(errors.keys())

    def test_action_delay_below_minimum_rejected(self, plugin):
        ok, _, errors = plugin.validateActionConfigUi(
            {"activityUuid": "A1", "delay": "0.5"}, "startActivity", 1
        )
        assert not ok
        assert "delay" in errors

    def test_action_delay_blank_ok(self, plugin):
        ok, _ = plugin.validateActionConfigUi(
            {"activityUuid": "A1", "delay": ""}, "startActivity", 1
        )
        assert ok

    def test_press_button_separator_rejected(self, plugin):
        ok, _, errors = plugin.validateActionConfigUi(
            {"button": "-1", "count": "1", "digits": ""}, "pressButton", 1
        )
        assert not ok
        assert "button" in errors

    def test_press_button_count_bounds(self, plugin):
        ok, _, errors = plugin.validateActionConfigUi(
            {"button": "VolumeUp", "count": "26", "digits": ""}, "pressButton", 1
        )
        assert not ok
        assert "count" in errors

    def test_device_config_requires_room(self, plugin):
        ok, _, errors = plugin.validateDeviceConfigUi({"roomUuid": ""}, "roomieRoom", 1)
        assert not ok
        assert "roomUuid" in errors

    def test_device_config_stores_room_name(self, plugin):
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        poller.poll_once()
        ok, values = plugin.validateDeviceConfigUi(
            {"roomUuid": "R1", "roomName": ""}, "roomieRoom", 1
        )
        assert ok
        assert values["roomName"] == "Living Room"


class TestDynamicLists:
    def test_room_list_from_cache_sorted(self, plugin):
        room2 = dict(ROOM_ON, roomuuid="R2", roomname="Attic")
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON, room2]))
        poller.poll_once()
        entries = plugin.get_room_list()
        assert entries == [("R2", "Attic"), ("R1", "Living Room")]

    def test_room_list_prepends_cached_selection(self, plugin):
        attach_poller(plugin, FakeClient(rooms=[]))
        entries = plugin.get_room_list(
            values_dict={"roomUuid": "R9", "roomName": "Old Room"}
        )
        assert entries[0] == ("R9", "Old Room (cached)")

    def test_activity_list_for_device(self, plugin):
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        poller.poll_once()
        dev = make_room_device(plugin)
        entries = plugin.get_activity_list(target_id=dev.id)
        assert ("A1", "Watch Plex") in entries
        assert ("A2", "Watch Netflix") in entries

    def test_activity_list_includes_toggle_variants(self, plugin):
        toggles = [
            {"uuid": "T1+", "name": "Shades (On)", "roomuuid": "R1"},
            {"uuid": "T1-", "name": "Shades (Off)", "roomuuid": "R1"},
        ]
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON], activities=toggles))
        poller.poll_once()
        dev = make_room_device(plugin)
        entries = plugin.get_activity_list(target_id=dev.id)
        assert ("T1+", "Shades (On)") in entries
        assert ("T1-", "Shades (Off)") in entries

    def test_button_list_grouped(self, plugin):
        entries = plugin.get_button_list()
        values = [value for value, _ in entries]
        assert "VolumeUp" in values
        assert "ActivityOff" in values
        assert values.count("-1") >= 8  # one separator per group
