from datetime import date
import re

from ...state.schemas import ResearchCategory, ResearchClassification


async def classify_research(state, reasoner):
    classification = await reasoner.ask(ResearchClassification,
        'Classify the research request by evidence requirements. Include academic whenever the user requests relevant papers, '
        'academic research where available, or related work. Set requires_code_inspection for repository-backed implementation comparisons.',
        {'request':state['request'],'today':date.today().isoformat()})
    if (ResearchCategory.ACADEMIC not in classification.categories
            and re.search(r'\b(?:academic research|academic papers|scholarly papers|peer-reviewed research)\b',
                          state['request'],re.I)):
        classification.categories.append(ResearchCategory.ACADEMIC)
    return {'classification':classification}
