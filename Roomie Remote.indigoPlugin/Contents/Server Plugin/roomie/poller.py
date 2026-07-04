"""Polling engine for Roomie room state.

Passive: the plugin's runConcurrentThread drives poll_once() and applies
the returned PollOutcome to Indigo devices. This module never imports
indigo, so the diffing/backoff logic is unit-testable.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from roomie.client import RoomieClient, RoomieError
from roomie.models import Activity, Room, normalize_room, room_to_state_activities


@dataclass
class PollOutcome:
    ok: bool
    error: Optional[str] = None
    activities_error: Optional[str] = None
    # room_uuid -> {stateKey: value} containing only states that changed
    room_state_changes: dict[str, dict] = field(default_factory=dict)
    rooms_present: set[str] = field(default_factory=set)
    # True on the first failed poll after successes and on the first
    # successful poll after failures.
    reachability_changed: bool = False


class RoomiePoller:
    def __init__(
        self,
        client: RoomieClient,
        poll_interval: float = 5.0,
        max_backoff: float = 60.0,
        activities_refresh_every: int = 12,
    ):
        self.poll_interval = float(poll_interval)
        self.max_backoff = float(max_backoff)
        self.activities_refresh_every = int(activities_refresh_every)
        self.wake_event = threading.Event()
        self._client = client
        self._lock = threading.Lock()
        self._rooms: dict[str, Room] = {}  # normalized, keyed by room uuid
        self._activities: dict[str, Activity] = {}  # keyed by uuid as received
        self._last_pushed: dict[str, dict] = {}  # room_uuid -> last emitted states
        self._failures = 0
        self._poll_count = 0
        self._refresh_activities = False

    def set_client(self, client: RoomieClient) -> None:
        with self._lock:
            self._client = client

    def request_refresh(self) -> None:
        self._refresh_activities = True
        self.wake_event.set()

    def next_delay(self) -> float:
        if self._failures == 0:
            return self.poll_interval
        return min(self.poll_interval * (2**self._failures), self.max_backoff)

    def rooms_snapshot(self) -> list[Room]:
        with self._lock:
            return list(self._rooms.values())

    def activities_for_room(self, room_uuid: str) -> list[Activity]:
        with self._lock:
            return [a for a in self._activities.values() if a.room_uuid == room_uuid]

    def off_activity_uuids(self) -> set[str]:
        with self._lock:
            return {a.uuid for a in self._activities.values() if a.is_off_type}

    def refresh_cache_now(self) -> list[Room]:
        """Synchronously fetch rooms + activities into the caches.

        For user-initiated dialogs/menus that need fresh data immediately.
        Does not touch the diff state, so the next poll_once still emits
        any state changes. Raises RoomieError on failure.
        """
        with self._lock:
            client = self._client
        raw_activities = client.get_activities()
        raw_rooms = client.get_rooms()
        with self._lock:
            self._activities = {
                a.uuid: a for a in (Activity.from_api(d) for d in raw_activities)
            }
        off_uuids = self.off_activity_uuids()
        rooms = [normalize_room(Room.from_api(d), off_uuids) for d in raw_rooms]
        with self._lock:
            self._rooms = {room.uuid: room for room in rooms}
        return rooms

    def poll_once(self, force_full: bool = False) -> PollOutcome:
        with self._lock:
            client = self._client
        try:
            raw_rooms = client.get_rooms()
        except RoomieError as exc:
            self._failures += 1
            return PollOutcome(
                ok=False,
                error=str(exc),
                reachability_changed=self._failures == 1,
            )

        recovered = self._failures > 0
        self._failures = 0
        self._poll_count += 1

        activities_error = None
        refresh_activities = (
            self._poll_count == 1
            or self._poll_count % self.activities_refresh_every == 0
            or self._refresh_activities
        )
        self._refresh_activities = False
        if refresh_activities:
            try:
                raw_activities = client.get_activities()
                with self._lock:
                    self._activities = {
                        a.uuid: a
                        for a in (Activity.from_api(d) for d in raw_activities)
                    }
            except RoomieError as exc:
                activities_error = str(exc)

        off_uuids = self.off_activity_uuids()
        rooms = [normalize_room(Room.from_api(d), off_uuids) for d in raw_rooms]
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")

        with self._lock:
            if force_full or recovered:
                self._last_pushed.clear()
            self._rooms = {room.uuid: room for room in rooms}
            changes: dict[str, dict] = {}
            for room in rooms:
                desired = self._room_states(room)
                previous = self._last_pushed.get(room.uuid, {})
                delta = {k: v for k, v in desired.items() if previous.get(k) != v}
                delta["lastPoll"] = timestamp
                changes[room.uuid] = delta
                self._last_pushed[room.uuid] = desired

        return PollOutcome(
            ok=True,
            activities_error=activities_error,
            room_state_changes=changes,
            rooms_present={room.uuid for room in rooms},
            reachability_changed=recovered,
        )

    @staticmethod
    def _room_states(room: Room) -> dict:
        return {
            "onOffState": not room.is_off,
            "currentActivityUuid": room.current_activity_uuid or "",
            "currentActivityName": room.current_activity_name or "",
            "roomName": room.name,
            "activityCount": len(room.activities),
            "activityList": json.dumps(room_to_state_activities(room)),
            "controllerReachable": True,
        }
