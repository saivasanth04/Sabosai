"""Read-only context lookups through GoModel's per-server MCP endpoints."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from gateway import API_KEY, GATEWAY_URL


class ContextLookupError(RuntimeError):
    """A requested source could not be retrieved."""


@asynccontextmanager
async def _connect(server: str):
    try:
        async with asyncio.timeout(75):
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {API_KEY}"}, timeout=70
            ) as http:
                # Each session sees the tools available when it initializes.
                for attempt in range(6):
                    transport = streamable_http_client(
                        f"{GATEWAY_URL}/mcp/{server}", http_client=http
                    )
                    async with Client(transport, read_timeout_seconds=65) as client:
                        if (await client.list_tools()).tools:
                            yield client
                            return
                    if attempt < 5:
                        await asyncio.sleep(1)
                raise ContextLookupError(
                    f"GoModel exposes no tools for '{server}'. "
                    "Check the MCP server configuration and credentials."
                )
    except Exception as exc:
        cause = exc
        while isinstance(cause, ExceptionGroup) and len(cause.exceptions) == 1:
            cause = cause.exceptions[0]
        if isinstance(cause, ContextLookupError):
            raise ContextLookupError(str(cause)) from exc
        raise ContextLookupError(
            f"{server} lookup failed. Check GoModel connectivity and the MCP server configuration."
        ) from exc


async def _call(client: Client, tool: str, arguments: dict) -> str:
    result = await client.call_tool(tool, arguments)
    if result.is_error:
        raise ContextLookupError(f"MCP tool '{tool}' returned an error.")
    text = "\n".join(item.text for item in result.content if item.type == "text")
    if not text and result.structured_content is not None:
        return json.dumps(result.structured_content, ensure_ascii=False)
    return text


async def _lookup(server: str, tool: str, arguments: dict) -> str:
    async with _connect(server) as client:
        return await _call(client, tool, arguments)


def inspect_workspace(workspace: str | None) -> str | None:
    if not workspace:
        return None
    listing = asyncio.run(_lookup("filesystem", "list_directory", {"path": workspace}))
    return listing[:12000] or "(empty directory)"


def inspect_github(repository_url: str) -> str:
    url = urlsplit(repository_url)
    parts = url.path.strip("/").split("/")
    if (
        url.scheme != "https" or url.netloc.lower() != "github.com"
        or len(parts) != 2
        or not all(re.fullmatch(r"[\w.-]+", part, re.ASCII) and part not in {".", ".."} for part in parts)
    ):
        raise ValueError("Provide a GitHub repository URL such as https://github.com/owner/repo.")
    owner, repo = parts
    repo = repo.removesuffix(".git")
    if repo in {"", ".", ".."}:
        raise ValueError("Provide a valid GitHub repository name.")
    return asyncio.run(_lookup("github", "get_file_contents", {
        "owner": owner, "repo": repo, "path": "/",
    }))[:12000]


async def _query_documentation(library: str, question: str) -> str:
    async with _connect("context7") as client:
        if not library.startswith("/"):
            matches = await _call(client, "resolve-library-id", {
                "libraryName": library, "query": question,
            })
            match = re.search(r"Context7-compatible library ID:\s*`?(/[\w./@-]+)", matches, re.I)
            if not match:
                raise ContextLookupError("Context7 could not resolve the library. Supply its exact library ID.")
            library = match.group(1)
        return await _call(client, "query-docs", {"libraryId": library, "query": question})


def query_context7(library: str, question: str) -> str:
    return asyncio.run(_query_documentation(library, question))[:12000]
