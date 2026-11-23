import asyncio
import json
from pathlib import Path

from .mcp_gateway import IntegrationError


class SwaggerParserAdapter:
    def __init__(self, timeout):
        self.timeout = timeout

    async def collect(self, spec_content):
        script = Path(__file__).with_name('parse_openapi.mjs')
        process = await asyncio.create_subprocess_exec(
            'node','--max-old-space-size=128',str(script),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:
            async with asyncio.timeout(self.timeout):
                output,_ = await process.communicate(spec_content.encode())
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode:
            raise IntegrationError('Swagger Parser rejected the specification or its runtime is missing')
        return json.loads(output)
