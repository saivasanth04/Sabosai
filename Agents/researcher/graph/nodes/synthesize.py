import json
import re

from ...state.schemas import ReportDraft, ResearchFinding, ResearchReport
from .cross_validate import summarize_evidence


async def synthesize_findings(state, reasoner):
    assessment = state['cross_validation']
    evidence = [item for item in state['evidence'] if item.source_type != 'search_snippet']
    if not evidence:
        raise RuntimeError('No citable evidence remains after cross-validation')
    narrative = [item for item in evidence if item.source_type in
                 {'source_code','documentation','paper_metadata','openapi','issue','retrieved_page'}]
    draft = await reasoner.ask(ReportDraft,
        'Prepare 4 to 12 concise, technically specific findings using only the source evidence. '
        'Each claim must include one or more exact evidence IDs in its evidence_ids field. Keep IDs out of claim text. '
        'Cite GitHub source files or Context7 for implementation claims. Use academic papers only when directly relevant. '
        'Leave versions, licenses, repository activity and relative popularity out of the generated claims. '
        'Do not conflate distinct projects or forks. Use concrete documented capabilities in each comparison. '
        'Assign each claim to finding, comparison, or recommendation. Make the first three claims a useful executive summary. '
        'When suitability is requested, include conditional recommendations tied to verified capabilities and explicit criteria. '
        'Use limitations for brief gaps only. Metadata findings are built separately by the application. '
        'Qualify version disagreement and uncertainty, and apply citation feedback. '
        'For recommendations, include separately grounded findings about suitability.',
        {'request':state['request'],'evidence':summarize_evidence(narrative or evidence,2900),
         'cross_validation':assessment.model_dump(),'versions':state['package_versions'],
         'inspection':[item.model_dump() for item in state.get('inspection_results',[])],
         'coverage':state['coverage'],'feedback':state.get('citation_feedback',[]),
         'previous_findings':[item.model_dump() for item in state['report'].findings] if 'report' in state else []})
    usable = [item for item in draft.findings if not re.search(
        r'\b(?:v?\d+\.\d+\.\d+|licen[cs](?:e|ed|ing)|archived|most frequently used|market share)\b',
        item.claim,re.I)]
    findings = [ResearchFinding(finding_id=f'f{index:02}', claim=item.claim,evidence_ids=item.evidence_ids)
                for index,item in enumerate(usable,1)]
    code = {item.evidence_id:item for item in evidence if item.source_type=='source_code'}
    inspected = set()
    for observation in state.get('inspection_results',[]):
        item = code.get(observation.evidence_id)
        if item and item.provenance not in inspected:
            inspected.add(item.provenance)
            findings.append(ResearchFinding(finding_id=f'f{len(findings)+1:02}',
                                            claim=observation.observation,evidence_ids=[item.evidence_id]))
    seen = set()
    for item in evidence:
        claim = None
        if item.source in {'pypi','npm'} and item.version:
            query = item.metadata['package']
            registry = {'pypi':'PyPI','npm':'npm'}[item.source]
            claim = f'{registry} lists {query} version {item.version}.'
        if item.source_type == 'repository_metadata':
            metadata = json.loads(item.content)
            license_id = (metadata.get('license') or {}).get('spdx_id')
            pushed_at = metadata.get('pushed_at')
            if license_id and license_id != 'NOASSERTION' and pushed_at:
                name = metadata['full_name']
                claim = f'GitHub lists {name} under {license_id} and records its last push on {pushed_at[:10]}.'
        if claim and claim not in seen:
            seen.add(claim)
            findings.append(ResearchFinding(finding_id=f'f{len(findings)+1:02}',claim=claim,evidence_ids=[item.evidence_id]))
    report = ResearchReport(findings=findings,executive_summary=[item.finding_id for item in findings[:3]],
                            comparisons=[finding.finding_id for finding,item in zip(findings,usable)
                                         if item.section=='comparison'],
                            recommendations=[finding.finding_id for finding,item in zip(findings,usable)
                                             if item.section=='recommendation'],limitations=draft.limitations)
    return {'report':report}
