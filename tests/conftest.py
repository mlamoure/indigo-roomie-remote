"""Pytest configuration: stub the `indigo` module before any plugin import.

The plugin runs inside Indigo's embedded Python where `indigo` is injected
by the host. Tests stub just the surface the plugin touches, following the
same approach as the Auto Lights plugin.
"""

import logging
import os
import sys
import types

indigo_stub = types.SimpleNamespace()


class _FolderList(list):
    """indigo.devices.folders — iterable of folder objects with .id/.name."""

    _next_id = 900

    def create(self, name):
        _FolderList._next_id += 1
        folder = types.SimpleNamespace(id=_FolderList._next_id, name=name)
        self.append(folder)
        return folder


class Devices(dict):
    """indigo.devices — iterable dict of devices keyed by id.

    Like the real DeviceList, membership also matches device names.
    """

    def __iter__(self):
        return iter(self.values())

    def iter(self, _filter=""):
        return iter(list(self.values()))

    def __contains__(self, key):
        return super().__contains__(key) or any(d.name == key for d in self.values())

    def __init__(self):
        super().__init__()
        self.folders = _FolderList()
        # real API: indigo.devices.folder.create(name)
        self.folder = types.SimpleNamespace(create=self.folders.create)


class Device:
    _next_id = 100

    def __init__(self, dev_id=None, name="", deviceTypeId="", pluginProps=None):
        if dev_id is None:
            Device._next_id += 1
            dev_id = Device._next_id
        self.id = dev_id
        self.name = name
        self.deviceTypeId = deviceTypeId
        self.pluginProps = pluginProps or {}
        self.states = {}
        self.errorState = None
        self.stateImage = None
        # recorded calls for assertions
        self.state_updates = []
        self.error_state_calls = []
        self.state_image_calls = []

    def updateStatesOnServer(self, state_list):
        self.state_updates.append(state_list)
        for item in state_list:
            self.states[item["key"]] = item["value"]

    def updateStateOnServer(self, key, value, uiValue=None):
        self.updateStatesOnServer([{"key": key, "value": value, "uiValue": uiValue}])

    def setErrorStateOnServer(self, message):
        self.errorState = message
        self.error_state_calls.append(message)

    def updateStateImageOnServer(self, image):
        self.stateImage = image
        self.state_image_calls.append(image)

    def stateListOrDisplayStateIdChanged(self):
        pass

    def replacePluginPropsOnServer(self, props):
        self.pluginProps = props


def _create_device(
    protocol=None,
    name="",
    deviceTypeId="",
    pluginId="",
    props=None,
    folder=0,
    **kwargs,
):
    dev = Device(name=name, deviceTypeId=deviceTypeId, pluginProps=dict(props or {}))
    dev.folderId = folder
    indigo_stub.devices[dev.id] = dev
    created_devices.append(dev)
    return dev


created_devices = []

indigo_stub.devices = Devices()
indigo_stub.Device = Device
indigo_stub.device = types.SimpleNamespace(create=_create_device)
indigo_stub.kProtocol = types.SimpleNamespace(Plugin="Plugin")
indigo_stub.kStateImageSel = types.SimpleNamespace(
    PowerOn="PowerOn", PowerOff="PowerOff"
)
indigo_stub.kUniversalAction = types.SimpleNamespace(RequestStatus="RequestStatus")
indigo_stub.kDeviceAction = types.SimpleNamespace(
    TurnOn="TurnOn", TurnOff="TurnOff", Toggle="Toggle"
)


class IndigoDict(dict):
    pass


indigo_stub.Dict = IndigoDict


class _DummyHandler(logging.Handler):
    def __init__(self, baseFilename="/tmp/Logs/plugin.log"):
        super().__init__()
        self.baseFilename = baseFilename

    def emit(self, record):
        pass


class _StopThread(Exception):
    pass


class PluginBase:
    StopThread = _StopThread

    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        self.pluginId = plugin_id
        self.pluginDisplayName = plugin_display_name
        self.pluginVersion = plugin_version
        self.pluginPrefs = plugin_prefs
        self.logger = logging.getLogger("Plugin")
        self.indigo_log_handler = _DummyHandler()
        self.plugin_file_handler = _DummyHandler()

    def sleep(self, seconds):
        pass

    def savePluginPrefs(self):
        pass


indigo_stub.PluginBase = PluginBase

sys.modules["indigo"] = indigo_stub

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            os.pardir,
            "Roomie Remote.indigoPlugin",
            "Contents",
            "Server Plugin",
        )
    ),
)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fake_indigo():
    indigo_stub.devices.clear()
    del indigo_stub.devices.folders[:]
    created_devices.clear()
    yield indigo_stub
