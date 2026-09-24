from dataclasses import dataclass, replace

from shop.models import Item, ShopError
from shop.steps import Discount, Step, Tax


@dataclass
class Cart:
    items: list[Item]
    total: float = 0.0

    @property
    def count(self) -> int:
        return len(self.items)


def add_prices(cart: Cart) -> Cart:
    return replace(cart, total=sum(item.price for item in cart.items))


# Prices the cart, then runs every step on it.
def checkout(cart: Cart) -> Cart:
    if not cart.count:
        raise ShopError("empty cart")
    cart = add_prices(cart)
    for step in STEPS:
        cart = step.apply(cart)
    return cart


STEPS: list[Step] = [Discount(), Tax()]
