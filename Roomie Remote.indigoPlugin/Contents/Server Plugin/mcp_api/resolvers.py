"""Resolve AI-supplied room / activity references to Roomie objects.

Pure functions over the poller's cached models. Matching is case-insensitive:
exact name first, then a unique substring. Anything ambiguous is a
`validation` error listing the matches; anything unknown is a `not_found`
error listing the candidates, so the caller can correct itself.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from roomie.models import Activity, Room

from .errors import ToolError


def _name_variants(name: str) -> list[str]:
    """`/activities` names are room-prefixed ("Living Room: Shades (On)");
    match on both the full name and the part after the prefix."""
    variants = [name]
    if ": " in name:
        variants.append(name.split(": ", 1)[1])
    return variants


def _match(items, text: str, names_of: Callable):
    needle = text.lower()
    exact, partial = [], []
    for item in items:
        variants = [v.lower() for v in names_of(item)]
        if needle in variants:
            exact.append(item)
        elif any(needle in v for v in variants):
            partial.append(item)
    return exact, partial


def resolve_room(
    rooms: Iterable[Room],
    room,
    room_uuid_for_device: Optional[Callable[[int], Optional[str]]] = None,
) -> Room:
    """`room` may be a room UUID, a room name, or an Indigo room-device id."""
    rooms = list(rooms)
    text = str(room or "").strip()
    if not text:
        raise ToolError(
            "validation",
            "room is required: a room name, room UUID, or Indigo room-device id",
        )
    if not rooms:
        raise ToolError(
            "not_found",
            "No Roomie rooms are cached; the controller may be unreachable "
            "(check roomie_get_status)",
        )
    candidates = [r.name for r in rooms]

    if text.isdigit() and room_uuid_for_device is not None:
        uuid = room_uuid_for_device(int(text))
        if uuid:
            for candidate in rooms:
                if candidate.uuid == uuid:
                    return candidate
            raise ToolError(
                "not_found",
                f"Indigo device {text} is linked to room {uuid}, which Roomie "
                "no longer reports",
                {"candidates": candidates},
            )

    for candidate in rooms:
        if candidate.uuid.lower() == text.lower():
            return candidate

    exact, partial = _match(rooms, text, lambda r: [r.name])
    if len(exact) == 1:
        return exact[0]
    matches = exact or partial
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ToolError(
            "validation",
            f"'{text}' matches several rooms; use the exact name or UUID",
            {"matches": [{"room_uuid": r.uuid, "name": r.name} for r in matches]},
        )
    raise ToolError(
        "not_found",
        f"No Roomie room matches '{text}'"
        + (" (and no Roomie room device has that Indigo id)" if text.isdigit() else ""),
        {"candidates": candidates},
    )


def _pick_variant(variants: list[Activity], toggle_state, text: str):
    """`variants` share one base UUID: the visible base entry and/or the
    '+'/'-' toggle entries. Returns (activity, ts_to_send)."""
    if toggle_state:
        for activity in variants:
            if activity.toggle_state == toggle_state:
                return activity, None  # the UUID suffix carries the state
        base = next((a for a in variants if a.toggle_state is None), variants[0])
        return base, toggle_state
    if len(variants) == 1:
        return variants[0], None
    base = [a for a in variants if a.toggle_state is None]
    if base:
        return base[0], None
    raise ToolError(
        "validation",
        f"'{text}' is a toggle activity; pass toggle_state 'on' or 'off', or "
        "use its '(On)' / '(Off)' name",
        {"matches": [{"uuid": a.uuid, "name": a.name} for a in variants]},
    )


def resolve_activity(
    room: Room,
    room_activities: Iterable[Activity],
    activity,
    toggle_state=None,
):
    """Resolve `activity` within `room`.

    `activity` may be a name (visible names, toggle '(On)'/'(Off)' names, or
    off-type names) or a UUID with or without a '+'/'-' toggle suffix.
    Returns (Activity, ts) where ts is the toggle state to send explicitly,
    or None when the chosen UUID already encodes it.
    """
    text = str(activity or "").strip()
    if not text:
        raise ToolError("validation", "activity is required: an activity name or UUID")
    if toggle_state not in (None, "on", "off"):
        raise ToolError("validation", "toggle_state must be 'on' or 'off'")

    pool = list(room.activities)
    seen = {a.uuid for a in pool}
    for extra in room_activities:
        if extra.uuid not in seen:
            pool.append(extra)
            seen.add(extra.uuid)
    if not pool:
        raise ToolError("not_found", f"Room '{room.name}' has no activities")

    lowered = text.lower()
    for candidate in pool:
        if candidate.uuid.lower() == lowered:
            return candidate, toggle_state if candidate.toggle_state is None else None
    base_matches = [a for a in pool if a.base_uuid.lower() == lowered]
    if base_matches:
        return _pick_variant(base_matches, toggle_state, text)

    exact, partial = _match(pool, text, lambda a: _name_variants(a.name))
    chosen = exact or partial
    if not chosen:
        raise ToolError(
            "not_found",
            f"No activity matches '{text}' in room '{room.name}'",
            {"candidates": [{"uuid": a.uuid, "name": a.name} for a in pool]},
        )
    groups: dict[str, list[Activity]] = {}
    for candidate in chosen:
        groups.setdefault(candidate.base_uuid, []).append(candidate)
    if len(groups) > 1:
        raise ToolError(
            "validation",
            f"'{text}' matches several activities in room '{room.name}'; use the "
            "exact name or UUID",
            {"matches": [{"uuid": a.uuid, "name": a.name} for a in chosen]},
        )
    return _pick_variant(next(iter(groups.values())), toggle_state, text)
