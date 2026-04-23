"""MCP stdio server — registers graph + vector + k8s + ticket tools."""
from __future__ import annotations

import asyncio
import json
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .tools import discovery, navigation, impact, build_test, k8s, memory, ticket


srv = Server("aiforge-graph")

TOOL_MODULES = [discovery, navigation, impact, build_test, k8s, memory, ticket]


@srv.list_tools()
async def list_tools() -> list[Tool]:
    out: list[Tool] = []
    for mod in TOOL_MODULES:
        for tool in mod.TOOLS:
            out.append(Tool(
                name=tool["name"],
                description=tool["description"],
                inputSchema=tool["input_schema"],
            ))
    return out


@srv.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    for mod in TOOL_MODULES:
        if name in mod.HANDLERS:
            result = mod.HANDLERS[name](arguments or {})
            if asyncio.iscoroutine(result):
                result = await result
            return [TextContent(type="text", text=json.dumps(result, default=str))]
    return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]


async def run() -> None:
    async with stdio_server() as (r, w):
        await srv.run(r, w, srv.create_initialization_options())


def main_entry() -> int:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
