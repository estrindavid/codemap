from shop import Item
from shop.models import Kind

DEFAULT_KIND = Kind.food


# Reads one item per line.
def load(path: str) -> list[Item]:
    return [Item(name=line.strip(), price=1.0, kind=DEFAULT_KIND) for line in open(path)]
