# Buy Price Fill Module

`youpin_buy_price_fill.py` owns the inventory buy-price backfill flow.

Public entry points:

- `buy_price_fill_config(config)`: parse the `buy_price_fill` config block.
- `update_purchase_price_fill_events(state, current_orders, config)`: detect filled purchase-order quantity changes.
- `apply_inferred_buy_prices_to_inventory(inventory, config, state)`: match new inventory items to fill events and infer missing buy prices.
- `queue_inferred_buy_price_fill_commands(inventory, config)`: queue Android emulator commands for matched inventory items.
- `refresh_status(orders, inventory, queued_commands, state)`: build the desktop/runner status summary.

Compatibility:

- `youpin_major_bidder.py` keeps the old function names as wrappers, so desktop and auto-runner code can keep calling the same API.
- Future buy-price-fill changes should start in `youpin_buy_price_fill.py`.

Current safety filter:

```json
{
  "buy_price_fill": {
    "stickers_only": true,
    "keywords": ["Cologne 2026"],
    "finish": "Holo"
  }
}
```

This keeps emulator backfill commands scoped to Cologne 2026 Holo stickers unless the config is intentionally changed.
