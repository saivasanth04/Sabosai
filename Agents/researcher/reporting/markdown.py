def generate_markdown(state):
    report = state['report']
    evidence = {item.evidence_id:item for item in state['evidence']}
    findings = {item.finding_id:item for item in report.findings}
    lines = ['# Research Report','',state['request'],'']
    refs = {}

    def render(item):
        citations = []
        for eid in item.evidence_ids:
            index = refs.setdefault(eid,len(refs)+1)
            citations.append(f'[E{index}]')
        return f"- {item.claim} {' '.join(citations)} (confidence: {item.confidence})"

    for title, items in [('Executive Summary',[findings[i] for i in report.executive_summary]),
                         ('Research Findings',report.findings),
                         ('Comparisons',[findings[i] for i in report.comparisons]),
                         ('Recommendations',[findings[i] for i in report.recommendations])]:
        if items:
            lines.extend([f'## {title}','',*(render(item) for item in items),''])
    lines.extend(['## Coverage and Limitations','',*(f'- {item}' for item in report.limitations)])
    for key,sources in state['coverage'].items():
        if sources:
            lines.append(f"- {key.replace('_',' ')}: {', '.join(sources)}")
    lines.extend(f'- {error.source}: {error.message}' for error in state['errors'])
    for row in state['cross_validation'].contradictions:
        urls = ', '.join(str(evidence[eid].url) for eid in row.evidence_ids)
        lines.append(f'- Evidence disagreement: {row.explanation} ({urls})')
    lines.extend(['','## References',''])
    for eid, index in refs.items():
        item = evidence[eid]
        lines.append(f'- [E{index}] {item.title} ({item.source}, retrieved {item.retrieved_at.date()}, evidence ID {eid})')
    lines.extend(['',*(f'[E{index}]: <{evidence[eid].url}>' for eid,index in refs.items())])
    return '\n'.join(lines)
