"""Researcher command line. Run with python -m Agents.researcher.main."""
import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx

from .config import load_settings
from .graph.workflow import build_researcher_graph
from .integrations.dispatcher import SourceDispatcher
from .integrations.mcp_gateway import MCPGateway, mcp_http
from .llm.client import Reasoner

ACCEPTANCE = ("Compare the leading open-source Python agent frameworks. Investigate their architecture, "
              "orchestration approach, tool integration, multi-agent support, parallel execution capabilities, "
              "recent development activity, licenses, official documentation, and current package versions. "
              "Identify their strengths, limitations, and suitable use cases. Support the conclusions with verifiable "
              "technical documentation, repository evidence, package information, and relevant academic research where available.")


@asynccontextmanager
async def create_researcher(settings=None):
    settings = settings or load_settings()
    async with httpx.AsyncClient(timeout=settings.timeout,trust_env=False) as http, mcp_http(settings) as mcp:
        gateway = MCPGateway(settings,mcp)
        await gateway.discover()
        dispatcher = SourceDispatcher(settings,gateway,http)
        yield build_researcher_graph(settings,dispatcher,Reasoner(settings))


async def run_research(query, progress=False):
    settings = load_settings()
    initial = {'request':query,'run_id':uuid4().hex,'evidence':[],'errors':[]}
    result = initial
    async with create_researcher(settings) as graph:
        async for state in graph.astream(initial,stream_mode='values',config={'max_concurrency':settings.concurrency}):
            result = state
            if progress:
                print(json.dumps({'run_id':state['run_id'],'state_keys':list(state),
                                  'evidence':len(state.get('evidence',[])),
                                  'errors':[item.model_dump() for item in state.get('errors',[])]}),
                      file=sys.stderr,flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request',nargs='?')
    parser.add_argument('--acceptance',action='store_true')
    parser.add_argument('--progress',action='store_true')
    args = parser.parse_args()
    if bool(args.request) == args.acceptance or args.request is not None and not args.request.strip():
        parser.error('Supply one nonempty request or use --acceptance')
    result = asyncio.run(run_research(ACCEPTANCE if args.acceptance else args.request,args.progress))
    if not result.get('output_files'):
        print(json.dumps(result.get('citation_feedback',[])),file=sys.stderr)
        return 2
    print(json.dumps(result['output_files'],indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
