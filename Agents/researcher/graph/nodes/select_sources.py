from ...research.source_selector import select_sources as plan_sources
from ...state.schemas import CollectionJob, SourceFailure


async def select_sources(state, dispatcher, settings, skills):
    jobs, errors = [], []
    available = dispatcher.registry.available()
    for question in state['questions']:
        selected, unavailable = plan_sources(question, settings, available)
        names = [settings.skills[source] for source in selected if source in settings.skills]
        for name in names:
            skills.load(name)
        jobs.append(CollectionJob(**question.model_dump(), selected_sources=selected,
                                  selected_skills=names, unavailable_sources=unavailable))
        for source in unavailable:
            message = dispatcher.gateway.failures.get(source, 'Source disabled or unconfigured')
            errors.append(SourceFailure(question_id=question.question_id, source=source, message=message))
    return {'jobs':jobs,'errors':errors}
