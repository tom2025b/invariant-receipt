"""A registry whose invariant is: the same ID can never be registered twice,
and a rejected registration stores nothing."""


class Registry:
    def __init__(self):
        self._seen = []

    def register(self, new_id):
        if new_id in self._seen:
            raise ValueError(f"duplicate id {new_id}")
        self._seen.append(new_id)

    def __len__(self):
        return len(self._seen)


def clamp_percent(value):
    if value < 0:
        return 0
    if value > 100:
        return 100
    return value
