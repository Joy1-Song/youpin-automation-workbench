from __future__ import annotations

from decimal import Decimal

import youpin_builtin_strategies as builtin


# Copy this file to strategies/my_strategy.py.
# Files whose names start with "_" are ignored by the strategy loader.

STRATEGY_ID = "my_strategy"
STRATEGY_NAME = "My Strategy"
STRATEGY_VERSION = "0.1.0"
DESCRIPTION = "Template strategy. Replace selection, pricing, and quantity logic."


DEFAULT_CONFIG = {
    "top_n": 10,
    "selection_strategy": {
        "mode": f"plugin:{STRATEGY_ID}",
        "min_on_sale_count": 5,
    },
    "bid_strategy": {
        "mode": f"plugin:{STRATEGY_ID}",
        "target_rank": 2,
        "rank_target_mode": "queue",
        "use_live_order_book": True,
        "use_book_top_price": True,
        "modify_interval_seconds": 60,
        "allow_create_orders": True,
        "tick": 0.1,
        "round_places": 1,
        "max_cycle_spend": 1000.0,
        "max_daily_spend": 1000.0,
    },
    "quantity_strategy": {
        "mode": f"plugin:{STRATEGY_ID}",
        "budget": 1000.0,
        "min_quantity_per_item": 3,
        "max_quantity_per_item": 5,
    },
    "purchase_risk": {
        "enabled": True,
        "rolling_success_limit": 1000.0,
        "lookback_hours": 24,
        "include_active_order_exposure": False,
        "active_order_exposure_multiplier": 2.0,
    },
}


DESKTOP_PROFILE = {
    "label": STRATEGY_NAME,
    "selection": f"plugin:{STRATEGY_ID}",
    "bid": f"plugin:{STRATEGY_ID}",
    "quantity": f"plugin:{STRATEGY_ID}",
    "create_channel": "emulator_queue",
    "allow_create": True,
    "budget": 1000,
    "min_quantity_per_item": 3,
    "max_quantity_per_item": 5,
    "max_quantity_hard_limit": 7,
    "active_order_exposure_multiplier": 2,
    "top_n": 10,
    "note": "Template strategy shown in the desktop strategy selector after copying and renaming.",
}


def select_candidates(candidates, config):
    """Return selected candidate templates in priority order."""
    return builtin.select_cheapest(candidates, config)[: int(config.get("top_n", 10))]


def calculate_bid_price(client, template, config, existing_order):
    """Return (price, reason). Return (None, reason) to skip this template."""
    return builtin.calculate_bid_price(client, template, config, existing_order)


def allocate_quantities(prices: list[Decimal], config):
    """Return one quantity per price. Zero means skip that item."""
    return builtin.allocate_quantities(prices, config)
