"""Roomie Universal Remote button lexicon.

The symbolic button names accepted by ``POST /api/v1/remote/press`` (Roomie X
10.3+, lexicon_version 1), grouped by the ``category`` that
``GET /api/v1/remote/capabilities`` reports for each one. Verified against a
live controller on 2026-09-07. No indigo imports.

The Press Remote Button action menu and the MCP ``press_button`` tool are
both driven from this one list so they can never drift apart.
"""

from __future__ import annotations

LEXICON_VERSION = 1

BUTTON_CATEGORIES: list[tuple[str, list[str]]] = [
    ("power", ["Power", "PowerOff", "PowerToggle"]),
    (
        "activity",
        ["ActivityOff", "ActivityPowerOff"] + [f"Activity{n}" for n in range(1, 9)],
    ),
    ("volume", ["VolumeUp", "VolumeDown", "VolumeMute", "VolumeMuteToggle"]),
    (
        "channel",
        ["ChannelUp", "ChannelDown", "ChannelPrevious", "ChannelEnter", "Channel"],
    ),
    (
        "transport",
        [
            "Play",
            "Pause",
            "PlayPause",
            "Stop",
            "Record",
            "FastForward",
            "Rewind",
            "SkipForward",
            "SkipBackward",
            "NextTrack",
            "PreviousTrack",
            "Replay",
            "Advance",
        ],
    ),
    (
        "cursor",
        [
            "Up",
            "Down",
            "Left",
            "Right",
            "Select",
            "Back",
            "Menu",
            "Home",
            "Exit",
            "Info",
            "Guide",
        ],
    ),
    ("numeric", [f"Digit{n}" for n in range(10)] + ["Dash", "Clear", "Enter"]),
    ("color", ["Red", "Green", "Yellow", "Blue"]),
    (
        "extended",
        [
            "PageUp",
            "PageDown",
            "DVR",
            "OnDemand",
            "Live",
            "Audio",
            "CC",
            "Subtitle",
            "AspectRatio",
            "PictureMode",
            "InputToggle",
            "A",
            "B",
            "C",
            "D",
        ],
    ),
]

CATEGORY_LABELS = {
    "power": "Power",
    "activity": "Activity",
    "volume": "Volume",
    "channel": "Channel",
    "transport": "Transport",
    "cursor": "Cursor / Navigation",
    "numeric": "Numeric",
    "color": "Color",
    "extended": "Extended",
}

BUTTON_NAMES: list[str] = [name for _, names in BUTTON_CATEGORIES for name in names]

BUTTON_CATEGORY_BY_NAME = {
    name: category for category, names in BUTTON_CATEGORIES for name in names
}

# Names this plugin offered before 2026.9.0 that Roomie never accepted, mapped
# to the lexicon name with the same meaning. Applied to saved actions so they
# keep working after the upgrade.
BUTTON_ALIASES = {
    "Mute": "VolumeMute",
    "SkipBack": "SkipBackward",
    "Input": "InputToggle",
}
BUTTON_ALIASES.update({str(n): f"Digit{n}" for n in range(10)})

# Pre-2026.9.0 names with no lexicon equivalent (value = suggested replacement).
RETIRED_BUTTONS = {
    "PowerOn": "Power or PowerToggle",
    "Dot": None,
    "Eject": None,
    "Search": None,
    "Settings": None,
}

_BY_LOWER = {name.lower(): name for name in BUTTON_NAMES}


class UnknownButton(ValueError):
    """The name is not a lexicon button, alias, or case variant."""


def normalize_button(name) -> str:
    """Map a user- or AI-supplied button name to its canonical lexicon name.

    Accepts canonical names, the pre-2026.9.0 aliases, and case variants.
    Raises UnknownButton (a ValueError) with a hint for anything else.
    """
    raw = str(name or "").strip()
    if raw in BUTTON_CATEGORY_BY_NAME:
        return raw
    if raw in BUTTON_ALIASES:
        return BUTTON_ALIASES[raw]
    if raw.lower() in _BY_LOWER:
        return _BY_LOWER[raw.lower()]
    if raw in RETIRED_BUTTONS:
        hint = RETIRED_BUTTONS[raw]
        suffix = f"; use {hint} instead" if hint else " and has no equivalent"
        raise UnknownButton(f"button '{raw}' is not in Roomie's lexicon{suffix}")
    raise UnknownButton(f"unknown button '{raw}'")
