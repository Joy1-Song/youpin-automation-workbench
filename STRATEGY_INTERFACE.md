# Strategy Interface

The project already supports strategy plugins. Put a Python file in `strategies/`, then point config modes at `plugin:<file_name>`.

## Required Functions

```python
def select_candidates(candidates, config):
    return candidates

def calculate_bid_price(client, template, config, existing_order):
    return price, reason

def allocate_quantities(prices, config):
    return quantities
```

## Desktop App Registration

Expose `DESKTOP_PROFILE` so the desktop app can show the strategy in the selector.

```python
DESKTOP_PROFILE = {
    "label": "My Strategy",
    "selection": "plugin:my_strategy",
    "bid": "plugin:my_strategy",
    "quantity": "plugin:my_strategy",
    "create_channel": "emulator_queue",
    "allow_create": True,
    "budget": 1000,
    "min_quantity_per_item": 3,
    "max_quantity_per_item": 5,
    "max_quantity_hard_limit": 7,
    "active_order_exposure_multiplier": 2,
    "top_n": 10,
    "note": "Shown in the desktop app.",
}
```

## Current Strategy Module

`strategies/cs2_2026_low_price_bid.py` is now the standalone module for the current "26 low-price Holo sticker buy-order" strategy.

It owns:

- candidate selection: cheapest Cologne 2026 Holo team stickers
- pricing: target buy-order rank, currently rank 2
- quantity: budget allocation with YouPin minimum quantity protection
- defaults: calendar-day filled-order risk limit, cleanup, CS update guard settings

## Exit Strategy Plugins

Exit strategy plugins live in the same `strategies/` directory. Configure them under `exit_strategy.plugins`:

```json
{
  "exit_strategy": {
    "auto_execute": false,
    "plugins": [
      {
        "mode": "plugin:cs2_2026_cologne_profit_exit",
        "enabled": true,
        "keywords": ["Cologne 2026"],
        "finish": "Holo",
        "min_profit_ratio": 0.5,
        "price_minus": 0.1,
        "target_active_items_per_template": 3,
        "max_reconcile_actions_per_cycle": 200
      }
    ]
  }
}
```

Each plugin must expose:

```python
def build_exit_actions(client, inventory, config, context) -> list[ExitAction]:
    ...
```

The current `cs2_2026_cologne_profit_exit` plugin reconciles inventory and existing listings. It targets at most 3 active listings per category, not 3 additional listings on every run. Correct listings are kept, surplus listings are removed, and stale prices are delisted before relisting. The candidate price is the lowest competing sell price minus 0.1; it must be at least 50% above the item's recorded cost (before fees). Missing costs and cooldown items are skipped. If the profit constraint no longer holds, existing listings are removed without replacement.

## Execution Queue

Strategies produce plan actions; the execution layer turns `create` actions into `create_purchase_order` commands in `state/emulator_commands.json` when `execution.create_order_channel` is `emulator_queue`.

The Android emulator executor now supports:

- `--process-create-orders --limit 10` for real APP buy-order creation
- `--process-buy-price-fill --limit 3` for inventory buy-price filling

The public example disables automatic processing and submission. Live operation needs the explicit live/configuration gates as well as both `execution.emulator_auto_process_create_orders` and `execution.emulator_auto_submit_create_orders`. Do not use the executor as an offline demonstration; run `python demo.py` instead.

For strategies that set `cleanup_strategy.reset_target_orders_each_cycle=true`, each cycle first cancels existing target-scope buy orders, then recreates the newly selected targets so the fresh APP orders start from `0/planned_quantity`.

## Adding A New Strategy

1. Copy `strategies/_template_strategy.py` to `strategies/my_strategy.py`.
2. Change `STRATEGY_ID`, `STRATEGY_NAME`, `DEFAULT_CONFIG`, and `DESKTOP_PROFILE`.
3. Implement the three required functions.
4. Restart the desktop app so it rescans `strategies/`.
5. Select the new strategy in the desktop app and run a dry run before live mode.

Plugins are ordinary trusted Python code, not sandboxed extensions. Do not load unreviewed third-party plugins. The execution bridge deduplicates active commands, but does not guarantee exactly-once remote execution after a crash. See [architecture and limits](docs/architecture.md).
