class SourceRegistry:

    def __init__(self):

        self.sources = {}

    def register(
        self,
        name: str,
        adapter
    ):

        if name in self.sources:
            raise ValueError(
                f"Duplicate source: {name}"
            )

        self.sources[name] = adapter

    def get(self, name):

        adapter = self.sources.get(name)

        if adapter is None:
            raise RuntimeError(
                f"Source not registered: {name}"
            )

        return adapter

    def available(self):

        return set(self.sources.keys())