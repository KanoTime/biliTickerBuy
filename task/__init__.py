"""
Task handlers for biliTickerBuy.
"""

from importlib import import_module

__all__ = [
    "Buy",
    "BuyConfig",
    "buy_new_terminal",
    "buy_stream",
    "BuyStreamEvent",
    "BuyStreamState",
    "BuyStreamUpdate",
    "BuyStreamWorker",
]


def __getattr__(name):
    # Loading a local goal/config/ledger must not also initialize the legacy
    # buyer, cookie store, log directories or network time synchronization.
    if name == "BuyConfig":
        module = import_module("app_cmd.config.BuyConfig")
    elif name in {"Buy", "buy_new_terminal", "buy_stream"}:
        module = import_module(".buy", __name__)
    elif name in {
        "BuyStreamEvent",
        "BuyStreamState",
        "BuyStreamUpdate",
        "BuyStreamWorker",
    }:
        module = import_module(".buy_types", __name__)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(module, name)
    globals()[name] = value
    return value
