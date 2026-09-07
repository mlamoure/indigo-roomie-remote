"""Tests for the shared Roomie button lexicon and legacy-name normalization."""

import pytest

from roomie.lexicon import (
    BUTTON_ALIASES,
    BUTTON_CATEGORIES,
    BUTTON_CATEGORY_BY_NAME,
    BUTTON_NAMES,
    CATEGORY_LABELS,
    RETIRED_BUTTONS,
    UnknownButton,
    normalize_button,
)


def test_lexicon_matches_live_controller_count():
    # 78 names, lexicon_version 1, captured from a live Roomie X controller
    # via /api/v1/remote/capabilities on 2026-09-07.
    assert len(BUTTON_NAMES) == 78
    assert len(set(BUTTON_NAMES)) == 78


def test_every_category_has_a_label():
    assert {c for c, _ in BUTTON_CATEGORIES} == set(CATEGORY_LABELS)


def test_category_lookup_covers_every_button():
    assert set(BUTTON_CATEGORY_BY_NAME) == set(BUTTON_NAMES)
    assert BUTTON_CATEGORY_BY_NAME["VolumeUp"] == "volume"
    assert BUTTON_CATEGORY_BY_NAME["Digit7"] == "numeric"


@pytest.mark.parametrize("name", ["VolumeUp", "ActivityOff", "Digit0", "D"])
def test_canonical_names_pass_through(name):
    assert normalize_button(name) == name


@pytest.mark.parametrize(
    "legacy,canonical",
    [("Mute", "VolumeMute"), ("SkipBack", "SkipBackward"), ("Input", "InputToggle")]
    + [(str(n), f"Digit{n}") for n in range(10)],
)
def test_legacy_aliases_map_to_lexicon(legacy, canonical):
    assert normalize_button(legacy) == canonical
    assert canonical in BUTTON_NAMES


def test_aliases_never_shadow_real_names():
    assert not set(BUTTON_ALIASES) & set(BUTTON_NAMES)


def test_case_insensitive_match():
    assert normalize_button("volumeup") == "VolumeUp"
    assert normalize_button(" playpause ") == "PlayPause"


@pytest.mark.parametrize("name", list(RETIRED_BUTTONS))
def test_retired_names_raise_with_hint(name):
    with pytest.raises(UnknownButton) as exc_info:
        normalize_button(name)
    assert "lexicon" in str(exc_info.value)


def test_unknown_raises_value_error():
    with pytest.raises(ValueError):
        normalize_button("Bogus")
    with pytest.raises(ValueError):
        normalize_button("")
