"""
MCP tool provider API for the Roomie Remote plugin.

Exposes room/activity/remote-control tools to the Indigo MCP Server plugin
(com.vtmikel.mcp_server) via the hidden `mcp_tool_invoke` action declared in
Actions.xml and advertised in Contents/Resources/mcp-manifest.json.

This package never imports `indigo`: everything the tools need from Indigo
arrives through the IndigoBridge callables that plugin.py injects, so the
tools are unit-testable exactly like the `roomie/` core.
"""
