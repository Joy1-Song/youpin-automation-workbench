# Price Alert Module

`youpin_price_alert.py` owns the inventory price-alert feature.

## Responsibilities

- Normalize `config["price_alert"]` defaults.
- Create, update, delete, and read alert rules.
- Parse bid/sale alert prices from desktop UI input.
- Check inventory market snapshots.
- Deduplicate already-sent alerts with `state/price_alert_state.json`.
- Send alert email through `youpin_daily_report.send_text_email`.

## Public Entry Points

- `ensure_alert_config(config, ...)`
- `parse_optional_price(value, field_name)`
- `upsert_rules_for_items(config, items, bid_target_price=..., sale_target_price=...)`
- `delete_rules_for_items(config, items)`
- `rule_map(config)`
- `rule_for_item(config, item)`
- `rule_targets(rule)`
- `check_price_alerts(client, config)`

Desktop code should call these functions instead of editing `price_alert.rules` directly.

## Rule Format

```json
{
  "enabled": true,
  "template_id": 122397,
  "market_hash_name": "Sticker | G2 Esports (Holo) | Cologne 2026",
  "name": "印花 | G2 esports (Holo) | Cologne 2026",
  "trigger": "split",
  "bid_target_price": 28.9,
  "sale_target_price": 33.0
}
```

`bid_target_price` triggers when the highest buy order reaches or exceeds the value.
`sale_target_price` triggers when the lowest market sell price reaches or exceeds the value.

Legacy `target_price` / `expected_price` / `price` rules are still readable for compatibility, but new desktop writes use the split fields only.
