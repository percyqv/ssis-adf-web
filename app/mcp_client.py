"""Client for the remote ssis-adf-agent MCP server (Streamable HTTP + API key)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


@dataclass
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any]


class McpError(RuntimeError):
    pass


class McpClient:
    """Opens a short-lived MCP session per operation (the server is stateless)."""

    def __init__(self, url: str, api_key: str, tools_ttl_seconds: int = 300) -> None:
        self.url = url if url.endswith("/mcp") else f"{url}/mcp"
        self._headers = {"X-API-Key": api_key}
        self._tools_cache: tuple[float, list[McpTool]] | None = None
        self._ttl = tools_ttl_seconds

    async def list_tools(self) -> list[McpTool]:
        if self._tools_cache and time.monotonic() - self._tools_cache[0] < self._ttl:
            return self._tools_cache[1]
        async with streamablehttp_client(self.url, headers=self._headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.list_tools()
        tools = [
            McpTool(t.name, t.description or "", dict(t.inputSchema or {"type": "object"}))
            for t in result.tools
        ]
        self._tools_cache = (time.monotonic(), tools)
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool. Returns parsed JSON when the tool answers with JSON text."""
        async with streamablehttp_client(self.url, headers=self._headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
        text = "\n".join(
            getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text"
        )
        if result.isError or text.startswith("Error:"):
            raise McpError(text.split("\n\nTraceback", 1)[0] or f"{name} failed")
        try:
            return json.loads(text)
        except ValueError:
            return text
