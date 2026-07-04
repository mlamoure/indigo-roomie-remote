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
        assert dev.name == "Living Room"
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

    def test_auto_create_disabled_still_records_known(self, plugin):
        plugin.pluginPrefs["autoCreateDevices"] = False
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        assert created_devices == []
        assert "R1" in json.loads(plugin.pluginPrefs["knownRoomUuids"])

    def test_existing_device_not_duplicated(self, plugin):
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        make_room_device(plugin, room_uuid="R1", name="My Living Room")
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        assert created_devices == []

    def test_name_collision_gets_suffix(self, plugin):
        # A non-plugin device already holds the room's name.
        other = indigo.Device(name="Living Room", deviceTypeId="relay")
        indigo.devices[other.id] = other
        poller = attach_poller(plugin, FakeClient(rooms=[ROOM_ON]))
        plugin._apply_outcome(poller.poll_once())
        from tests.conftest import created_devices

        assert created_devices[0].name == "Living Room 2"

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
