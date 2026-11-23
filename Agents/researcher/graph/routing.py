from langgraph.graph import END
from langgraph.types import Send


def dispatch_collection(state):
    return [Send('collect_evidence',{'job':job}) for job in state['jobs']]


def route_after_citations(state, settings):
    if state['citation_passed']:
        return 'export_report'
    if state['citation_attempts'] < settings.citation_attempts:
        return 'synthesize_findings'
    return END
