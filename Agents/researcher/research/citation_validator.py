"""Deterministic citation bindings and semantic claim review."""
import asyncio
import re

from ..state.schemas import CitationValidation
from ..graph.nodes.cross_validate import summarize_evidence

LITERALS = re.compile(r'https?://[^\s<>\])]+|\b\d{4}-\d{2}-\d{2}\b|\b\d+\.\d+\.\d+(?:[a-z]+\d+)?\b')


async def validate_citations(state, reasoner, max_attempts=2):
    report = state['report']
    assessment = state['cross_validation']
    conflicts = {eid for row in assessment.contradictions for eid in row.evidence_ids}
    records = {item.evidence_id:item for item in state['evidence']
               if item.source_type!='search_snippet'}
    findings = {item.finding_id:item for item in report.findings}
    issues, candidates = [], []
    if len(findings)!=len(report.findings):
        issues.append('Duplicate finding IDs')
    for field in ('executive_summary','comparisons','recommendations'):
        if unknown := set(getattr(report,field))-findings.keys():
            issues.append(f'{field} must contain finding IDs only: {", ".join(sorted(unknown))}')
    for finding in findings.values():
        ids = set(finding.evidence_ids)
        if not ids <= records.keys():
            issues.append(f'{finding.finding_id} cites unknown or snippet-only evidence')
            continue
        evidence = [records[eid] for eid in sorted(ids)]
        text = '\n'.join(f'{item.url}\n{item.content}\n{item.published_at}' for item in evidence)
        if any(literal.rstrip('.,;') not in text for literal in LITERALS.findall(finding.claim)):
            issues.append(f'{finding.finding_id} has a date, URL or version absent from cited evidence')
            continue
        provenance = {item.provenance for item in evidence if item.evidence_id not in conflicts}
        finding.confidence = 'high' if len(provenance)>1 else 'medium' if provenance else 'low'
        candidates.append({'id':finding.finding_id,'claim':finding.claim,
                           'recommendation':finding.finding_id in report.recommendations,
                           'evidence':summarize_evidence(evidence,4200)})

    async def audit(batch):
        return await reasoner.ask(CitationValidation,
            'Audit every ID once. Evaluate the claim against only its attached evidence; reject any detail not entailed, '
            'missing context or unrelated source. A conditional recommendation may be inferred from directly documented '
            'capabilities without requiring the source to make the same recommendation. Its factual premises still need support. '
            'Return specific correction advice.',
            {'claims':batch})
    audits = [item for result in await asyncio.gather(*(audit(candidates[i:i+5]) for i in range(0,len(candidates),5)))
              for item in result.audits]
    expected = {row['id'] for row in candidates}
    complete = len(audits)==len(expected) and {item.finding_id for item in audits}==expected
    if not complete:
        issues.append('Semantic audit did not cover every valid finding exactly once')
    issues.extend(f'{item.finding_id}: {item.explanation}' for item in audits if not item.supported)
    attempt = state.get('citation_attempts',0)+1
    passed = not issues
    if issues and complete and attempt >= max_attempts and len(findings)==len(report.findings):
        supported = {item.finding_id for item in audits if item.supported}
        if supported:
            retained = [item for item in report.findings if item.finding_id in supported]
            sections = {label:[fid for fid in getattr(report,label) if fid in supported]
                        for label in ('executive_summary','comparisons','recommendations')}
            if not sections['executive_summary']:
                sections['executive_summary'] = [item.finding_id for item in retained[:3]]
            report = report.model_copy(update={'findings':retained,**sections,
                'limitations':report.limitations+[f'{len(findings)-len(retained)} findings were omitted after citation review.']})
            passed = True
    return {'report':report,'citation_passed':passed,'citation_feedback':issues,
            'citation_attempts':attempt,'citation_audits':[item.model_dump() for item in audits]}
