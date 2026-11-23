from ...state.schemas import CrossValidation


def summarize_evidence(records, limit=1900):
    return [{**item.model_dump(mode='json',exclude={'content'}),'content':item.content[:limit]} for item in records]


async def cross_validate(state, reasoner, settings):
    evidence = state.get('evidence',[])
    if len(evidence) < settings.minimum_evidence:
        errors = '; '.join(f'{item.source}: {item.message}' for item in state.get('errors',[]))
        raise RuntimeError(f'Insufficient evidence. {errors}')
    assessment = await reasoner.ask(CrossValidation,
        'Identify real disagreement between the supplied evidence records and records irrelevant to the research task. '
        'Distinguish incomplete evidence from disagreement. Return known evidence IDs only.',
        {'request':state['request'],'evidence':summarize_evidence(evidence,800)})
    valid = {item.evidence_id for item in evidence}
    assessment.contradictions = [row for row in assessment.contradictions
                                 if len(set(row.evidence_ids))>=2 and set(row.evidence_ids)<=valid]
    assessment.unrelated_evidence_ids = [item for item in assessment.unrelated_evidence_ids if item in valid]
    versions = {}
    for item in evidence:
        if item.source_type=='package_metadata' and item.version:
            key = f"{item.metadata['ecosystem']}:{item.metadata['package']}"
            versions.setdefault(key,{})[item.source] = item.version
    used = {item.source for item in evidence}
    selected = {source for job in state['jobs'] for source in job.selected_sources}
    unavailable = {source for job in state['jobs'] for source in job.unavailable_sources}
    return {'cross_validation':assessment,'package_versions':versions,
            'coverage':{'used':sorted(used),'selected_without_evidence':sorted(selected-used),
                        'unavailable':sorted(unavailable),
                        'not_selected':sorted((set(settings.endpoints)|set(settings.mcp_servers)|{'fetch','swagger_parser'})
                                              -selected-used-unavailable)}}
