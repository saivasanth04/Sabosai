"""Namespaced, allowlisted MCP discovery and calls through GoModel."""
import asyncio
import json
from contextlib import asynccontextmanager

import httpx2
from jsonschema import validate
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from ..config import gateway_key


class IntegrationError(RuntimeError):
    pass


class MCPGateway:
    def __init__(self, settings, http):
        self.settings, self.http = settings, http
        self.catalog = {}
        self.failures = {}

    @asynccontextmanager
    async def connect(self, server):
        url = f'{self.settings.gateway}/mcp/{server}'
        transport = streamable_http_client(url, http_client=self.http)
        async with Client(transport, read_timeout_seconds=self.settings.timeout) as client:
            yield client

    async def discover(self):
        async def discover_server(server, expected):
            try:
                async with asyncio.timeout(min(15, self.settings.timeout)), self.connect(server) as client:
                    catalog = {}
                    cursor = None
                    while True:
                        page = await client.list_tools(cursor=cursor)
                        catalog.update({tool.name: tool.input_schema for tool in page.tools if tool.name in expected})
                        cursor = page.next_cursor
                        if not cursor:
                            break
                    if not catalog:
                        raise IntegrationError('GoModel advertised no allowed tools')
                    self.catalog[server] = catalog
                    if missing := (set(expected) - catalog.keys()):
                        self.failures[server] = 'Missing tools: ' + ', '.join(sorted(missing))
            except Exception as exc:
                self.failures[server] = str(exc) if isinstance(exc, IntegrationError) else type(exc).__name__
        await asyncio.gather(*(discover_server(server, tools) for server, tools in self.settings.mcp_servers.items()))

    async def invoke(self, server, tool, arguments):
        schema = self.catalog.get(server, {}).get(tool)
        if schema is None:
            raise IntegrationError(f'{server}/{tool} is unavailable')
        validate(arguments, schema)
        async with self.connect(server) as client:
            result = await client.call_tool(tool, arguments)
        if result.is_error:
            raise IntegrationError(f'{server}/{tool} returned an MCP error')
        data = result.structured_content
        if data is not None:
            return data.get('result', data)
        texts = [item.text if item.type == 'text' else item.resource.text
                 for item in result.content if item.type == 'text'
                 or item.type == 'resource' and hasattr(item.resource, 'text')]
        text = '\n'.join(texts).strip()
        if not text:
            raise IntegrationError(f'{server}/{tool} returned no content')
        try:
            return json.loads(text)
        except ValueError:
            return text


def mcp_http(settings):
    return httpx2.AsyncClient(timeout=settings.timeout,
                              headers={'Authorization':f'Bearer {gateway_key()}'})
