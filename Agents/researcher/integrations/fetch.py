"""Bounded HTTPS fetch with public DNS validation and per-hop address pinning."""
import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

from .mcp_gateway import IntegrationError


async def public_address(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username is not None or parts.password is not None or parts.port not in (None, 443):
        raise IntegrationError('Expected a public HTTPS URL without credentials')
    host = parts.hostname.encode('idna').decode('ascii')
    records = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    if not addresses or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in addresses):
        raise IntegrationError('URL resolves to a private or non-public address')
    return host, str(addresses[0])


async def fetch(http, url, limit, allowed=('text/', 'json', 'yaml', 'xml')):
    for _ in range(4):
        host, address = await public_address(url)
        parts = urlsplit(url)
        pinned = parts._replace(netloc=f'[{address}]' if ':' in address else address).geturl()
        async with http.stream('GET', pinned, headers={'Host':host}, extensions={'sni_hostname':host}) as response:
            if response.is_redirect and response.headers.get('location'):
                url = urljoin(url, response.headers['location'])
                continue
            response.raise_for_status()
            mime = response.headers.get('content-type', '').lower()
            if not any(kind in mime for kind in allowed):
                raise IntegrationError('Unexpected response media type')
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > limit:
                    raise IntegrationError('Source response exceeds configured size limit')
            return url, content.decode(response.encoding or 'utf-8', errors='replace'), mime
    raise IntegrationError('Redirect limit exceeded')
