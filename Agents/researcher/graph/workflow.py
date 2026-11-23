"""Build the nine-node evidence graph with application-owned dependencies."""
from functools import partial

from langgraph.graph import END, START, StateGraph

from .nodes.classify import classify_research
from .nodes.collect_evidence import collect_evidence
from .nodes.cross_validate import cross_validate
from .nodes.decompose import decompose_research
from .nodes.export import export_report
from .nodes.inspect_implementations import inspect_implementations
from .nodes.select_sources import select_sources
from .nodes.synthesize import synthesize_findings
from .nodes.validate_citations import validate_citations
from .routing import dispatch_collection, route_after_citations
from ..skills.registry import SkillRegistry
from ..state.research_state import ResearchState


def build_researcher_graph(settings, dispatcher, reasoner):
    graph = StateGraph(ResearchState)
    nodes = {
        'classify_research':partial(classify_research,reasoner=reasoner),
        'decompose_research':partial(decompose_research,reasoner=reasoner,settings=settings),
        'select_sources':partial(select_sources,dispatcher=dispatcher,settings=settings,skills=SkillRegistry()),
        'collect_evidence':partial(collect_evidence,dispatcher=dispatcher,reasoner=reasoner),
        'inspect_implementations':partial(inspect_implementations,dispatcher=dispatcher,reasoner=reasoner),
        'cross_validate':partial(cross_validate,reasoner=reasoner,settings=settings),
        'synthesize_findings':partial(synthesize_findings,reasoner=reasoner),
        'validate_citations':partial(validate_citations,reasoner=reasoner,max_attempts=settings.citation_attempts),
        'export_report':partial(export_report,settings=settings),
    }
    for name,node in nodes.items():
        graph.add_node(name,node)
    for source,target in zip((START,*nodes),(*nodes,END)):
        if source in {'select_sources','validate_citations'}:
            continue
        graph.add_edge(source,target)
    graph.add_conditional_edges('select_sources',dispatch_collection,['collect_evidence'])
    graph.add_conditional_edges('validate_citations',partial(route_after_citations,settings=settings),
                                ['synthesize_findings','export_report',END])
    return graph.compile()
