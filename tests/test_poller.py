"""Tests for RoomiePoller: diffing, backoff, reachability edges, wake event."""

import json

import pytest

from roomie.client import RoomieUnreachable
from roomie.poller import RoomiePoller


class FakeClient:
    """Scriptable stand-in for RoomieClient."""

    def __init__(self, rooms=None, activities=None):
        self.rooms = rooms if rooms is not None else []
        self.activities = activities if activities is not None else []
        self.fail_rooms = False

    def get_rooms(self):
        if self.fail_rooms:
            raise RoomieUnreachable("connection refused")
        return self.rooms

    def get_activities(self):
        return self.activities


ROOM_ON = {
    "roomuuid": "R1",
    "roomname": "Living Room",
    "currentactivityuuid": "A1",
    "currentactivityname": "Watch Plex",
    "activities": [
        {"activityuuid": "A1", "name": "Watch Plex", "ic": "logo-plex"},
        {"activityuuid": "A2", "name": "Watch Netflix", "ic": "logo-netflix"},
    ],
}

ROOM_OFF = {
    "roomuuid": "R1",
    "roomname": "Living Room",
    "currentactivityuuid": "OFF1",
    "currentactivityname": "System Off",
    "currentactivityoff": True,
    "activities": [
        {"activityuuid": "A1", "name": "Watch Plex", "ic": "logo-plex"},
        {"activityuuid": "A2", "name": "Watch Netflix", "ic": "logo-netflix"},
    ],
}

OFF_ACTIVITY = {
    "uuid": "OFF1",
    "name": "Living Room: System Off",
    "type": "off",
    "roomuuid": "R1",
}


def make_poller(client, **kwargs):
    kwargs.setdefault("poll_interval", 5.0)
    return RoomiePoller(client, **kwargs)


class TestDiffing:
    def test_first_poll_emits_full_state(self):
        poller = make_poller(FakeClient(rooms=[ROOM_ON]))
        outcome = poller.poll_once()
        assert outcome.ok
        changes = outcome.room_state_changes["R1"]
        assert changes["onOffState"] is True
        assert changes["currentActivityUuid"] == "A1"
        assert changes["currentActivityName"] == "Watch Plex"
        assert changes["roomName"] == "Living Room"
        assert changes["activityCount"] == 2
        assert json.loads(changes["activityList"]) == [
            {"uuid": "A1", "name": "Watch Plex"},
            {"uuid": "A2", "name": "Watch Netflix"},
        ]
        assert changes["controllerReachable"] is True
        assert "lastPoll" in changes

    def test_unchanged_poll_emits_only_lastpoll(self):
        poller = make_poller(FakeClient(rooms=[ROOM_ON]))
        poller.poll_once()
        outcome = poller.poll_once()
        assert set(outcome.room_state_changes["R1"].keys()) == {"lastPoll"}

    def test_activity_change_emits_only_affected_keys(self):
        client = FakeClient(rooms=[ROOM_ON])
        poller = make_poller(client)
        poller.poll_once()
        changed = dict(ROOM_ON)
        changed["currentactivityuuid"] = "A2"
        changed["currentactivityname"] = "Watch Netflix"
        client.rooms = [changed]
        outcome = poller.poll_once()
        assert set(outcome.room_state_changes["R1"].keys()) == {
            "currentActivityUuid",
            "currentActivityName",
            "lastPoll",
        }

    def test_off_transition_normalizes(self):
        client = FakeClient(rooms=[ROOM_ON])
        poller = make_poller(client)
        poller.poll_once()
        client.rooms = [ROOM_OFF]
        outcome = poller.poll_once()
        changes = outcome.room_state_changes["R1"]
        assert changes["onOffState"] is False
        assert changes["currentActivityUuid"] == ""
        assert changes["currentActivityName"] == ""

    def test_force_full_re_emits_everything(self):
        poller = make_poller(FakeClient(rooms=[ROOM_ON]))
        poller.poll_once()
        outcome = poller.poll_once(force_full=True)
        assert "onOffState" in outcome.room_state_changes["R1"]
        assert "activityList" in outcome.room_state_changes["R1"]

    def test_room_rename_emits_room_name(self):
        client = FakeClient(rooms=[ROOM_ON])
        poller = make_poller(client)
        poller.poll_once()
        renamed = dict(ROOM_ON)
        renamed["roomname"] = "Great Room"
        client.rooms = [renamed]
        outcome = poller.poll_once()
        assert outcome.room_state_changes["R1"]["roomName"] == "Great Room"

    def test_disappeared_room_excluded_from_present(self):
        client = FakeClient(rooms=[ROOM_ON])
        poller = make_poller(client)
        assert poller.poll_once().rooms_present == {"R1"}
        client.rooms = []
        assert poller.poll_once().rooms_present == set()


class TestOffTypeFallback:
    def test_older_controller_off_detected_via_activities(self):
        # currentactivityoff absent, but the current activity is a known
        # off-type from /activities.
        room = dict(ROOM_ON)
        room["currentactivityuuid"] = "OFF1"
        room["currentactivityname"] = "System Off"
        client = FakeClient(rooms=[room], activities=[OFF_ACTIVITY])
        poller = make_poller(client)
        outcome = poller.poll_once()
        changes = outcome.room_state_changes["R1"]
        assert changes["onOffState"] is False
        assert changes["currentActivityName"] == ""

    def test_activities_error_does_not_fail_poll(self):
        client = FakeClient(rooms=[ROOM_ON])

        def boom():
            raise RoomieUnreachable("activities down")

        client.get_activities = boom
        poller = make_poller(client)
        outcome = poller.poll_once()
        assert outcome.ok
        assert outcome.activities_error is not None


class TestBackoffAndReachability:
    def test_backoff_progression_and_cap(self):
        client = FakeClient(rooms=[ROOM_ON])
        poller = make_poller(client, poll_interval=5.0, max_backoff=60.0)
        assert poller.next_delay() == 5.0
        client.fail_rooms = True
        expected = [10.0, 20.0, 40.0, 60.0, 60.0]
        for delay in expected:
            poller.poll_once()
            assert poller.next_delay() == delay

    def test_reachability_changed_only_on_edges(self):
        client = FakeClient(rooms=[ROOM_ON])
        poller = make_poller(client)
        assert poller.poll_once().reachability_changed is False
        client.fail_rooms = True
        assert poller.poll_once().reachability_changed is True
        assert poller.poll_once().reachability_changed is False
        client.fail_rooms = False
        assert poller.poll_once().reachability_changed is True
        assert poller.poll_once().reachability_changed is False

    def test_recovery_resets_delay_and_forces_full_push(self):
        client = FakeClient(rooms=[ROOM_ON])
        poller = make_poller(client)
        poller.poll_once()
        client.fail_rooms = True
        poller.poll_once()
        client.fail_rooms = False
        outcome = poller.poll_once()
        assert poller.next_delay() == 5.0
        # Full state re-emitted after recovery, not just lastPoll.
        assert "onOffState" in outcome.room_state_changes["R1"]

    def test_failed_poll_has_no_state_changes(self):
        client = FakeClient(rooms=[ROOM_ON])
        client.fail_rooms = True
        outcome = make_poller(client).poll_once()
        assert not outcome.ok
        assert outcome.room_state_changes == {}
        assert "refused" in outcome.error


class TestCachesAndWake:
    def test_rooms_snapshot(self):
        poller = make_poller(FakeClient(rooms=[ROOM_ON]))
        poller.poll_once()
        rooms = poller.rooms_snapshot()
        assert [r.uuid for r in rooms] == ["R1"]
        assert rooms[0].current_activity_name == "Watch Plex"

    def test_activities_for_room(self):
        other = {"uuid": "X1", "name": "Other: Thing", "roomuuid": "R2"}
        poller = make_poller(
            FakeClient(rooms=[ROOM_ON], activities=[OFF_ACTIVITY, other])
        )
        poller.poll_once()
        assert [a.uuid for a in poller.activities_for_room("R1")] == ["OFF1"]

    def test_request_refresh_sets_wake_event(self):
        poller = make_poller(FakeClient())
        assert not poller.wake_event.is_set()
        poller.request_refresh()
        assert poller.wake_event.is_set()

    def test_activities_refreshed_on_request_refresh(self):
        client = FakeClient(rooms=[ROOM_ON], activities=[])
        poller = make_poller(client, activities_refresh_every=1000)
        poller.poll_once()
        client.activities = [OFF_ACTIVITY]
        poller.poll_once()
        assert poller.activities_for_room("R1") == []  # not yet refreshed
        poller.request_refresh()
        poller.poll_once()
        assert [a.uuid for a in poller.activities_for_room("R1")] == ["OFF1"]
