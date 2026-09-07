"""
Dispatcher for the `mcp_tool_invoke` hidden action.

The MCP Server plugin calls
executeAction("mcp_tool_invoke", props={"tool": <name>, "arguments": <JSON string>})
and receives a JSON string back. Arguments cross the boundary as a JSON
string because indigo.Dict cannot carry None values or $-prefixed keys.

Reply envelope (always a JSON string, never an exception):
  {"status": "ok", "result": <any JSON>}
  {"status": "error", "error": {"type": "validation|not_found|conflict|internal",
                                "message": str, "details": <optional JSON>}}
"""

import inspect
import json
import logging
from typing import Any, Dict

from .errors import ToolError
from .tool_service import RoomieToolService

logger = logging.getLogger("Plugin")


class ToolDispatcher:
    def __init__(self, service: RoomieToolService):
        self._service = service
        self._tools = {
            "list_rooms": service.list_rooms,
            "get_room": service.get_room,
            "get_capabilities": service.get_capabilities,
            "list_devices": service.list_devices,
            "get_status": service.get_status,
            "start_activity": service.start_activity,
            "power_off_room": service.power_off_room,
            "press_button": service.press_button,
        }

    def tool_names(self):
        return sorted(self._tools)

    def dispatch(self, tool_name: str, arguments_json: str) -> str:
        try:
            handler = self._tools.get(tool_name)
            if handler is None:
                return self._error(
                    "not_found",
                    f"Unknown tool {tool_name!r}; available: {', '.join(self.tool_names())}",
                )

            try:
                arguments = json.loads(arguments_json or "{}")
            except json.JSONDecodeError as e:
                return self._error("validation", f"arguments is not valid JSON: {e}")
            if not isinstance(arguments, dict):
                return self._error("validation", "arguments must be a JSON object")

            try:
                inspect.signature(handler).bind(**arguments)
            except TypeError as e:
                return self._error("validation", f"bad arguments for {tool_name}: {e}")

            logger.info(f"MCP tool invoked: {tool_name}")
            result = handler(**arguments)
            return json.dumps({"status": "ok", "result": result})
        except ToolError as e:
            return self._error(e.error_type, e.message, e.details)
        except Exception as e:
            logger.exception(f"MCP tool {tool_name!r} failed")
            return self._error("internal", f"{type(e).__name__}: {e}")

    @staticmethod
    def _error(error_type: str, message: str, details: Any = None) -> str:
        error: Dict[str, Any] = {"type": error_type, "message": message}
        if details is not None:
            error["details"] = details
        logger.warning(f"MCP tool error ({error_type}): {message}")
        return json.dumps({"status": "error", "error": error})
