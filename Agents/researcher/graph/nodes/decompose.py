from datetime import date
from ...state.schemas import Decomposition, ResearchCategory, ResearchQuestion


async def decompose_research(state, reasoner, settings):
    result = await reasoner.ask(Decomposition,
        'Plan distinct evidence-oriented research questions. For a comparison of Python agent frameworks include one web-only '
        'discovery question and two or three specific framework questions covering repository, documentation, and library evidence. '
        'For each framework supply its exact PyPI distribution, ecosystem pypi, official documentation URL, GitHub repo URL, '
        'and a known implementation symbol. Use null for unknown fields. Include academic discovery if requested. '
        'Keep exact GitHub issue URLs when supplied for an issue inspection. '
        'Keep to the supplied limit. Do not invent URLs or package names.',
        {'request':state['request'],'classification':state['classification'].model_dump(mode='json'),
         'max_questions':settings.max_questions,'today':date.today().isoformat()})
    seen = set()
    questions = []
    for question in result.questions:
        key = question.question.casefold()
        if key not in seen and len(questions) < settings.max_questions:
            categories = list(question.categories)
            indicated = [(question.repository_url,ResearchCategory.REPOSITORY),
                         (question.documentation_url,ResearchCategory.DOCUMENTATION),
                         (question.package_name and question.package_ecosystem,ResearchCategory.LIBRARY)]
            categories = list(dict.fromkeys(categories+[category for value,category in indicated if value]))
            question = question.model_copy(update={'question_id':f'q{len(questions)+1:02}',
                                                    'categories':categories})
            questions.append(question)
            seen.add(key)
    if (ResearchCategory.ACADEMIC in state['classification'].categories
            and not any(ResearchCategory.ACADEMIC in question.categories for question in questions)):
        if len(questions) == settings.max_questions:
            questions.pop()
        subjects = ', '.join(dict.fromkeys(item.subject for item in questions if item.subject and item.package_name))
        query = (f'Published studies or benchmarks on agent orchestration for {subjects}' if subjects
                 else state['classification'].summary)
        questions.append(ResearchQuestion(question_id=f'q{len(questions)+1:02}',question=query,
             categories=[ResearchCategory.ACADEMIC],subject=subjects or None,package_name=None,
             package_ecosystem=None,repository_url=None,documentation_url=None,api_spec_url=None,code_symbol=None))
    return {'questions':questions}
