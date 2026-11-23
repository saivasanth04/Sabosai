def generate_json(state):
    def serialize(value):
        if hasattr(value,'model_dump'):
            return value.model_dump(mode='json')
        if isinstance(value,list):
            return [serialize(item) for item in value]
        return value
    return {key:serialize(value) for key,value in state.items() if key not in {'job','output_files'}}
