from dataclasses import dataclass
from enum import Enum


class Kind(Enum):
    food = "food"
    tool = "tool"


# One thing in the shop.
@dataclass
class Item:
    name: str
    price: float
    kind: Kind


class ShopError(Exception):
    pass
