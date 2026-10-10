from __future__ import annotations

import time
from typing import Any, Callable

import youpin_api as api
import youpin_major_bidder as bidder

LogFn = Callable[[str], None]


def inventory_key(item: api.InventoryItem) -> str:
    return item.market_hash_name or item.name


def fetch_existing_orders(client: api.YouPinClient) -> dict[int, api.ExistingPurchaseOrder]:
    return bidder.fetch_existing_orders(client)


def fetch_inventory_items(
    client: api.YouPinClient,
    config: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> list[api.InventoryItem]:
    return bidder.fetch_inventory_items(client, config, state=state)


def build_market_snapshot(client: api.YouPinClient, item: api.InventoryItem) -> api.MarketSnapshot:
    return bidder.build_market_snapshot(client, item)


def fallback_market_snapshot(item: api.InventoryItem) -> api.MarketSnapshot:
    return api.MarketSnapshot(
        template_id=item.template_id,
        name=item.name,
        market_hash_name=item.market_hash_name,
        lowest_sell_price=item.reference_price,
        highest_purchase_price=None,
        on_sale_count=None,
        raw={},
    )


def fetch_inventory_market_snapshots(
    client: api.YouPinClient,
    inventory: list[api.InventoryItem],
    config: dict[str, Any],
    *,
    log: LogFn | None = None,
) -> dict[str, api.MarketSnapshot]:
    unique_items: dict[str, api.InventoryItem] = {}
    for item in inventory:
        unique_items.setdefault(inventory_key(item), item)
    if not unique_items:
        return {}

    market_config = config.get("inventory_market", {})
    request_interval = float(market_config.get("request_interval_seconds", 0.2))
    snapshots: dict[str, api.MarketSnapshot] = {}
    if log:
        log(f"开始刷新库存盘口：{len(unique_items)} 个品类")
    for idx, (key, item) in enumerate(unique_items.items(), start=1):
        try:
            snapshots[key] = build_market_snapshot(client, item)
        except Exception as exc:
            if log:
                log(f"盘口刷新失败：{item.name} | {exc}")
            snapshots[key] = fallback_market_snapshot(item)
        if request_interval > 0 and idx < len(unique_items):
            time.sleep(request_interval)
    if log:
        log(f"库存盘口已刷新：{len(snapshots)} 个品类")
    return snapshots
