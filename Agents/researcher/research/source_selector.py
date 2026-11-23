"""Selection based on structured intent and live capabilities."""


def select_sources(question, settings, available):
    requested = list(dict.fromkeys(source for category in question.categories
                                   for source in settings.routes[category.value]))
    if 'deps_dev' in requested and question.package_ecosystem:
        requested.append(question.package_ecosystem)
    if 'duckduckgo' in requested and 'duckduckgo' not in available and 'exa' in available:
        requested.append('exa')
    active = [source for source in requested if source in available]
    unavailable = [source for source in requested if source not in available]
    return active, unavailable
