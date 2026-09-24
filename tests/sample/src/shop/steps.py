from abc import ABC, abstractmethod
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shop.cart import Cart


class Step(ABC):
    @abstractmethod
    def apply(self, cart: "Cart") -> "Cart":
        ...


class Discount(Step):
    def apply(self, cart: "Cart") -> "Cart":
        return replace(cart, total=cart.total * 0.9)


class Tax(Step):
    def apply(self, cart: "Cart") -> "Cart":
        return replace(cart, total=round(cart.total * 1.08, 2))
