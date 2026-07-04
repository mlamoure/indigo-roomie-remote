"""Tests for roomie.models parsing: toggle suffixes, optional fields,
currentactivityoff semantics."""

from roomie.models import Activity, Room, normalize_room, room_to_state_activities

LIVE_ROOM_OFF = {
    "roomuuid": "8BC011E6-9F7C-49F9-BADD-04CCD21804D0",
    "roomcolor": 5,
    "roomname": "Basement Bar & TV Area",
    "currentactivityuuid": "928FCFE3-875B-4C89-A59A-D682199F10D5",
    "currentactivityname": "System Off",
    "currentactivityoff": True,
    "activities": [
        {"name": "Watch Zidoo", "ic": "logo-zidoo", "activityuuid": "AAA"},
        {"name": "Play XBox", "ic": "logo-xbox", "activityuuid": "BBB"},
    ],
}

LIVE_ROOM_ON = {
    "roomuuid": "1DAC99E8-30D2-46ED-9D29-FCDD8AF7BDD1",
    "roomcolor": 1,
    "roomname": "Living Room",
    "currentactivityuuid": "43F98357",
    "currentactivityname": "Watch AppleTV",
    "activities": [
        {"name": "Watch Plex", "ic": "logo-plex", "activityuuid": "CCC"},
        {"name": "Watch AppleTV", "ic": "logo-appletv", "activityuuid": "43F98357"},
    ],
}

LIVE_ROOM_EMPTY = {
    "roomname": "Server Area",
    "roomuuid": "E83C3C1C",
    "roomcolor": 3,
    "activities": [],
}


class TestActivity:
    def test_toggle_on_suffix(self):
        activity = Activity.from_api({"uuid": "ABC-123+", "name": "Shades (On)"})
        assert activity.toggle_state == "on"
        assert activity.is_toggle
        assert activity.base_uuid == "ABC-123"
        assert activity.uuid == "ABC-123+"

    def test_toggle_off_suffix(self):
        activity = Activity.from_api({"uuid": "ABC-123-", "name": "Shades (Off)"})
        assert activity.toggle_state == "off"
        assert activity.is_toggle
        assert activity.base_uuid == "ABC-123"

    def test_plain_uuid_not_toggle(self):
        activity = Activity.from_api({"uuid": "ABC-123", "name": "Watch TV"})
        assert activity.toggle_state is None
        assert not activity.is_toggle
        assert activity.base_uuid == "ABC-123"

    def test_off_type(self):
        activity = Activity.from_api(
            {"uuid": "DDD", "name": "Room: System Off", "type": "off"}
        )
        assert activity.is_off_type

    def test_optional_fields_absent(self):
        activity = Activity.from_api({"uuid": "DDD", "name": "Minimal"})
        assert activity.icon is None
        assert activity.room_uuid is None
        assert not activity.is_off_type

    def test_activities_endpoint_shape(self):
        activity = Activity.from_api(
            {
                "uuid": "261FD083",
                "name": "Basement: Watch Zidoo",
                "icon": "logo-zidoo",
                "roomuuid": "8BC011E6",
                "vuuid": "V",
                "openuuid": "O",
                "guideuuid": "G",
                "auxuuids": ["A1"],
            }
        )
        assert activity.uuid == "261FD083"
        assert activity.icon == "logo-zidoo"
        assert activity.room_uuid == "8BC011E6"

    def test_room_embedded_shape(self):
        activity = Activity.from_api(
            {"activityuuid": "AAA", "name": "Watch Zidoo", "ic": "logo-zidoo"},
            room_uuid="ROOM1",
        )
        assert activity.uuid == "AAA"
        assert activity.icon == "logo-zidoo"
        assert activity.room_uuid == "ROOM1"


class TestRoom:
    def test_off_room_normalizes_activity_fields(self):
        room = Room.from_api(LIVE_ROOM_OFF)
        assert room.is_off is True
        assert room.current_activity_uuid is None
        assert room.current_activity_name is None
        assert room.name == "Basement Bar & TV Area"
        assert room.color == 5
        assert [a.name for a in room.activities] == ["Watch Zidoo", "Play XBox"]

    def test_on_room_missing_off_key(self):
        room = Room.from_api(LIVE_ROOM_ON)
        assert room.is_off is None  # key absent, activity running
        assert room.current_activity_uuid == "43F98357"
        assert room.current_activity_name == "Watch AppleTV"

    def test_empty_room_is_off(self):
        room = Room.from_api(LIVE_ROOM_EMPTY)
        assert room.is_off is True
        assert room.activities == []

    def test_activity_order_preserved(self):
        room = Room.from_api(LIVE_ROOM_ON)
        assert [a.uuid for a in room.activities] == ["CCC", "43F98357"]

    def test_embedded_activities_inherit_room_uuid(self):
        room = Room.from_api(LIVE_ROOM_ON)
        assert all(a.room_uuid == room.uuid for a in room.activities)


class TestEffectiveOff:
    def test_explicit_off_wins(self):
        room = Room.from_api(LIVE_ROOM_OFF)
        assert room.effective_off(set()) is True

    def test_fallback_uses_off_uuid_set(self):
        room = Room.from_api(LIVE_ROOM_ON)
        assert room.effective_off(set()) is False
        assert room.effective_off({"43F98357"}) is True

    def test_normalize_room_clears_fields_via_fallback(self):
        room = normalize_room(Room.from_api(LIVE_ROOM_ON), {"43F98357"})
        assert room.is_off is True
        assert room.current_activity_uuid is None
        assert room.current_activity_name is None

    def test_normalize_room_keeps_running_activity(self):
        room = normalize_room(Room.from_api(LIVE_ROOM_ON), set())
        assert room.is_off is False
        assert room.current_activity_name == "Watch AppleTV"


def test_room_to_state_activities():
    room = Room.from_api(LIVE_ROOM_OFF)
    assert room_to_state_activities(room) == [
        {"uuid": "AAA", "name": "Watch Zidoo"},
        {"uuid": "BBB", "name": "Play XBox"},
    ]
