# The command line: loads the items, checks out the cart, prints the total.
from . import loader
from .cart import Cart, checkout
from shop.models import Kind


def main() -> None:
    items = loader.load("items.txt")
    cart = Cart(items=items)
    cart = checkout(cart)
    print(cart.total, Kind.food)
