"""Drift guards between Contents/Resources/mcp-manifest.json, the dispatcher's
tool table, the shared button lexicon, and Info.plist."""

import json
import os
import plistlib

import pytest

from mcp_api.tool_dispatcher import ToolDispatcher
from mcp_api.tool_service import RoomieToolService
from roomie.lexicon import BUTTON_NAMES

PLUGIN_ROOT = os.path.join(
    os.path.dirname(__file__), "..", "Roomie Remote.indigoPlugin", "Contents"
)

EXPECTED = {
    # name: (write, timeout)
    "list_rooms": (False, 15),
    "get_room": (False, 15),
    "get_capabilities": (False, 20),
    "list_devices": (False, 20),
    "get_status": (False, 20),
    "start_activity": (True, 30),
    "power_off_room": (True, 30),
    "press_button": (True, 30),
}


@pytest.fixture(scope="module")
def manifest():
    with open(os.path.join(PLUGIN_ROOT, "Resources", "mcp-manifest.json")) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def info_plist():
    with open(os.path.join(PLUGIN_ROOT, "Info.plist"), "rb") as f:
        return plistlib.load(f)


def tool(manifest, name):
    return next(t for t in manifest["tools"] if t["name"] == name)


def test_manifest_basics(manifest, info_plist):
    assert manifest["manifest_version"] == 1
    assert manifest["provider"]["plugin_id"] == info_plist["CFBundleIdentifier"]
    assert manifest["tool_prefix"] == "roomie"
    assert manifest["invoke_action_id"] == "mcp_tool_invoke"


def test_manifest_tools_match_dispatcher(manifest):
    dispatcher = ToolDispatcher(RoomieToolService(poller=None, client_factory=None))
    assert sorted(t["name"] for t in manifest["tools"]) == dispatcher.tool_names()
    assert set(EXPECTED) == set(dispatcher.tool_names())


def test_write_flags_and_timeouts(manifest):
    for name, (write, timeout) in EXPECTED.items():
        entry = tool(manifest, name)
        assert entry["write"] is write, name
        assert entry["timeout_seconds"] == timeout, name
        assert 5 <= entry["timeout_seconds"] <= 120


def test_every_schema_is_a_documented_object(manifest):
    for entry in manifest["tools"]:
        assert entry["description"], entry["name"]
        schema = entry["inputSchema"]
        assert schema["type"] == "object", entry["name"]
        assert set(schema["required"]) <= set(schema["properties"]), entry["name"]
        for prop_name, prop in schema["properties"].items():
            assert prop.get("description"), f"{entry['name']}.{prop_name}"
            assert prop.get("type"), f"{entry['name']}.{prop_name}"


def test_button_enum_is_the_shared_lexicon(manifest):
    button = tool(manifest, "press_button")["inputSchema"]["properties"]["button"]
    assert button["enum"] == BUTTON_NAMES


def test_room_argument_required_where_it_matters(manifest):
    for name in (
        "get_room",
        "get_capabilities",
        "start_activity",
        "power_off_room",
        "press_button",
    ):
        assert "room" in tool(manifest, name)["inputSchema"]["required"], name
    assert tool(manifest, "start_activity")["inputSchema"]["required"] == [
        "room",
        "activity",
    ]
    assert tool(manifest, "press_button")["inputSchema"]["required"] == [
        "room",
        "button",
    ]


def test_actions_xml_declares_the_invoke_action():
    with open(os.path.join(PLUGIN_ROOT, "Server Plugin", "Actions.xml")) as f:
        xml = f.read()
    assert 'id="mcp_tool_invoke"' in xml
    assert 'uiPath="hidden"' in xml
    assert "handle_mcp_tool_invoke" in xml
