import asyncio

from ...state.schemas import Inspections
from ...integrations.dispatcher import resolved_repository
from .collect_evidence import failure


async def inspect_implementations(state, dispatcher, reasoner):
    async def inspect(job):
        job = resolved_repository(job,state.get('evidence',[]))
        extra, errors = [], []
        if (job.repository_url and 'github' in job.selected_sources
                and (job.code_symbol or state['classification'].requires_code_inspection)):
            try:
                targets = await dispatcher.code_targets(job)
                for target in targets:
                    try:
                        extra.extend(await dispatcher.collect('github',job,target))
                    except Exception as exc:
                        errors.append(failure(job,'github',exc))
                if not targets:
                    from ...integrations.mcp_gateway import IntegrationError
                    raise IntegrationError('GitHub code search returned no commit-pinned file candidates')
            except Exception as exc:
                errors.append(failure(job,'github',exc))
        records = extra + [item for item in state.get('evidence',[]) if item.question_id==job.question_id
                           and item.source_type in {'openapi','documentation','package_metadata','paper_metadata'}]
        observations = []
        if records:
            try:
                result = await reasoner.ask(Inspections,'Return concise relevant observations and exact consecutive excerpts. '
                                            'Every excerpt must occur verbatim in its cited evidence.',
                                            {'question':job.question,'evidence':{item.evidence_id:item.content[:4500] for item in records}})
                contents = {item.evidence_id:item.content for item in records}
                observations = [item for item in result.observations if item.excerpt in contents.get(item.evidence_id,'')]
            except Exception as exc:
                errors.append(failure(job,'inspection',exc))
        return extra, observations, errors

    results = await asyncio.gather(*(inspect(job) for job in state['jobs']))
    return {key:[item for result in results for item in result[index]]
            for index,key in enumerate(('evidence','inspection_results','errors'))}
