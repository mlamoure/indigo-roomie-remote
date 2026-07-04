"""Dataclasses for Roomie Remote API objects.

Parsing is deliberately tolerant: the Roomie API omits keys rather than
sending nulls, so every optional field is presence-tested. No indigo
imports — this module is unit-testable outside Indigo.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Optional

# /api/v1/activities marks off-type activities with "type": "off"
# (confirmed against a live Roomie X controller).
OFF_ACTIVITY_TYPES = {"off"}


@dataclass
class Activity:
    uuid: str  # as received, including trailing "+"/"-" for toggle variants
    base_uuid: str  # uuid with any toggle suffix stripped
    name: str
    room_uuid: Optional[str] = None
    icon: Optional[str] = None
    is_toggle: bool = False
    toggle_state: Optional[str] = None  # "on" / "off" / None
    is_off_type: bool = False
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, data: dict, room_uuid: Optional[str] = None) -> "Activity":
        """Parse an activity from either API shape.

        Room-embedded entries (GET /rooms) use activityuuid/name/ic;
        top-level entries (GET /activities) use uuid/name/icon/roomuuid.
        """
        uuid = data.get("uuid") or data.get("activityuuid") or ""
        toggle_state = None
        base_uuid = uuid
        if uuid.endswith("+"):
            toggle_state = "on"
            base_uuid = uuid[:-1]
        elif uuid.endswith("-"):
            toggle_state = "off"
            base_uuid = uuid[:-1]
        return cls(
            uuid=uuid,
            base_uuid=base_uuid,
            name=data.get("name", ""),
            room_uuid=data.get("roomuuid", room_uuid),
            icon=data.get("icon") or data.get("ic"),
            is_toggle=toggle_state is not None or bool(data.get("toggle")),
            toggle_state=toggle_state,
            is_off_type=data.get("type") in OFF_ACTIVITY_TYPES,
            raw=data,
        )


@dataclass
class Room:
    uuid: str
    name: str
    color: Optional[int] = None
    is_off: Optional[bool] = None  # None => currentactivityoff key absent
    current_activity_uuid: Optional[str] = None
    current_activity_name: Optional[str] = None
    activities: list[Activity] = field(default_factory=list)
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, data: dict) -> "Room":
        uuid = data.get("roomuuid") or data.get("uuid") or ""
        current_uuid = data.get("currentactivityuuid")
        current_name = data.get("currentactivityname")
        if "currentactivityoff" in data:
            is_off = bool(data["currentactivityoff"])
        elif current_uuid is None:
            # No current activity at all (e.g. a room with no activities)
            # is unambiguously "nothing running".
            is_off = True
        else:
            # Key absent on older controllers; caller may resolve via the
            # off-type cross-check (see Room.effective_off).
            is_off = None
        if is_off:
            current_uuid = None
            current_name = None
        return cls(
            uuid=uuid,
            name=data.get("roomname") or data.get("name") or "",
            color=data.get("roomcolor"),
            is_off=is_off,
            current_activity_uuid=current_uuid,
            current_activity_name=current_name,
            activities=[
                Activity.from_api(a, room_uuid=uuid) for a in data.get("activities", [])
            ],
            raw=data,
        )

    def effective_off(self, off_activity_uuids: set[str]) -> bool:
        """Resolve on/off, falling back to known off-type activity UUIDs
        when the controller didn't send currentactivityoff."""
        if self.is_off is not None:
            return self.is_off
        if self.current_activity_uuid is None:
            return True
        return self.current_activity_uuid in off_activity_uuids


def room_to_state_activities(room: Room) -> list[dict[str, str]]:
    """The JSON-serializable {uuid, name} list exposed in the
    activityList device state, preserving Roomie's visible order."""
    return [{"uuid": a.uuid, "name": a.name} for a in room.activities]


def normalize_room(room: Room, off_activity_uuids: set[str]) -> Room:
    """Return a copy with is_off resolved and activity fields normalized
    to None when the room is (effectively) off."""
    is_off = room.effective_off(off_activity_uuids)
    return dataclasses.replace(
        room,
        is_off=is_off,
        current_activity_uuid=None if is_off else room.current_activity_uuid,
        current_activity_name=None if is_off else room.current_activity_name,
    )
