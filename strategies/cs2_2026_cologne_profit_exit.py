from __future__ import annotations

import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

from youpin_api import ExitAction, MarketSnapshot
from youpin_strategy_sdk import money, normalize_text, to_decimal


STRATEGY_ID = "cs2_2026_cologne_profit_exit"
STRATEGY_NAME = "CS2 2026 Cologne profit exit"
STRATEGY_VERSION = "0.2.0"
DESCRIPTION = (
    "Exit strategy for Cologne 2026 Holo stickers: when an item is tradeable "
    "and the list price still has at least 50% profit over buy price, reconcile "
    "the user's active listings to exactly 3 items per category at the lowest "
    "competitor sell price minus 0.1."
)


DEFAULT_CONFIG = {
    "exit_strategy": {
        "plugins": [
            {
                "mode": f"plugin:{STRATEGY_ID}",
                "enabled": True,
                "keywords": ["Cologne 2026"],
                "finish": "Holo",
                "stickers_only": True,
                "min_profit_ratio": 0.5,
                "price_minus": 0.1,
                "target_active_items_per_template": 3,
                "max_reconcile_actions_per_cycle": 200,
                "sale_book_page_size": 100,
                "sale_book_max_pages": 20,
                "round_places": 1,
                "min_list_price": 0.1,
            }
        ]
    }
}


EXIT_STRATEGY_PROFILE = {
    "label": STRATEGY_NAME,
    "mode": f"plugin:{STRATEGY_ID}",
    "note": "Cologne 2026 Holo, >=50% profit, reconcile each category to 3 active listings at competitor lowest - 0.1.",
}


def _spec(context: dict[str, Any]) -> dict[str, Any]:
    value = context.get("plugin_config", {})
    return value if isinstance(value, dict) else {}


def _decimal(value: Any, default: str) -> Decimal:
    parsed = to_decimal(value)
    return parsed if parsed is not None else Decimal(default)


def _round_down(value: Decimal, places: int) -> Decimal:
    quantum = Decimal("1").scaleb(-places)
    return value.quantize(quantum, rounding=ROUND_DOWN)


def _text(item: Any) -> str:
    return f"{getattr(item, 'market_hash_name', '')} {getattr(item, 'name', '')}"


def _category_key(item: Any) -> str:
    template_id = getattr(item, "template_id", None)
    if template_id not in {None, ""}:
        return f"template:{template_id}"
    return normalize_text(getattr(item, "market_hash_name", "") or getattr(item, "name", ""))


def _is_target_item(item: Any, spec: dict[str, Any]) -> bool:
    text = normalize_text(_text(item))
    if bool(spec.get("stickers_only", True)) and "sticker" not in text:
        return False

    keywords = spec.get("keywords", ["Cologne 2026"])
    if not isinstance(keywords, list):
        keywords = ["Cologne 2026"]
    if any(normalize_text(str(keyword)) not in text for keyword in keywords if str(keyword).strip()):
        return False

    finish = str(spec.get("finish", "Holo")).strip()
    if finish and normalize_text(finish) not in text:
        return False
    return True


def _is_tradeable_now(item: Any) -> bool:
    if bool(getattr(item, "is_on_sale", False)):
        return False
    if getattr(item, "asset_status", None) not in {None, 0}:
        return False
    if getattr(item, "tradable", None) is False:
        return False
    if str(getattr(item, "cooldown_desc", "") or "").strip():
        return False
    return True


def _snapshot_for(client: Any, item: Any, config: dict[str, Any], context: dict[str, Any]):
    snapshots = context.setdefault("snapshots", {})
    key = getattr(item, "market_hash_name", "") or getattr(item, "name", "") or getattr(item, "asset_id", "")
    if key in snapshots:
        return snapshots[key]

    builder = context.get("build_market_snapshot")
    if not callable(builder):
        raise RuntimeError("exit plugin context missing build_market_snapshot")
    snapshot = builder(client, item)
    snapshots[key] = snapshot

    interval = _decimal((config.get("inventory_market", {}) or {}).get("request_interval_seconds"), "0.2")
    if interval > 0:
        time.sleep(float(interval))
    return snapshot


def _commodity_id(item: Any) -> str:
    value = getattr(item, "commodity_id", "")
    if value:
        return str(value)
    raw = getattr(item, "raw", {})
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("CommodityId", raw.get("commodityId", "")) or "")


def _sale_id(record: dict[str, Any]) -> str:
    for key in ("Id", "id", "CommodityId", "commodityId", "CommodityNo", "commodityNo"):
        value = record.get(key)
        if value not in {None, ""}:
            return str(value)
    return ""


def _sale_price(record: dict[str, Any]) -> Decimal | None:
    for key in ("Price", "price", "SellPrice", "sellPrice", "SalePrice", "salePrice"):
        value = to_decimal(record.get(key))
        if value is not None and value > 0:
            return value
    return None


def _same_price(left: Decimal | None, right: Decimal, places: int) -> bool:
    return left is not None and _round_down(left, places) == right


def _profit_ok(item: Any, list_price: Decimal, min_profit_ratio: Decimal) -> bool:
    buy_price = getattr(item, "buy_price", None)
    if buy_price is None or buy_price <= 0:
        return False
    return list_price >= buy_price * (Decimal("1") + min_profit_ratio)


def _action_rule(
    min_profit_ratio: Decimal,
    price_minus: Decimal,
    target_count: int,
    current_price: Decimal | None,
) -> dict[str, Any]:
    return {
        "source": STRATEGY_ID,
        "mode": f"plugin:{STRATEGY_ID}",
        "min_profit_ratio": str(min_profit_ratio),
        "price_minus": str(price_minus),
        "target_active_items_per_template": target_count,
        "current_sale_price": str(current_price) if current_price is not None else None,
    }


def build_category_reconciliation_actions(
    items: list[Any],
    sale_book: list[dict[str, Any]],
    spec: dict[str, Any],
) -> list[ExitAction]:
    if not items:
        return []
    min_profit_ratio = _decimal(spec.get("min_profit_ratio"), "0.5")
    price_minus = _decimal(spec.get("price_minus"), "0.1")
    min_list_price = _decimal(spec.get("min_list_price"), "0.1")
    places = int(spec.get("round_places", 1))
    target_count = max(
        1,
        int(
            spec.get(
                "target_active_items_per_template",
                spec.get("max_items_per_template_per_cycle", 3),
            )
        ),
    )

    listed = [item for item in items if bool(getattr(item, "is_on_sale", False))]
    listed_by_commodity = {_commodity_id(item): item for item in listed if _commodity_id(item)}
    own_prices: dict[str, Decimal] = {}
    competitor_prices: list[Decimal] = []
    for record in sale_book:
        if not isinstance(record, dict):
            continue
        commodity_id = _sale_id(record)
        price = _sale_price(record)
        is_own = commodity_id in listed_by_commodity or record.get("IsMine") is True or record.get("isMine") is True
        if is_own:
            if commodity_id in listed_by_commodity and price is not None:
                own_prices[commodity_id] = price
        elif price is not None:
            competitor_prices.append(price)

    if competitor_prices:
        market_anchor = min(competitor_prices)
        list_price = _round_down(market_anchor - price_minus, places)
    elif own_prices:
        market_anchor = min(own_prices.values())
        list_price = _round_down(market_anchor, places)
    else:
        return []
    if list_price < min_list_price:
        return []

    first = items[0]
    snapshot = MarketSnapshot(
        template_id=getattr(first, "template_id", None),
        name=getattr(first, "name", ""),
        market_hash_name=getattr(first, "market_hash_name", ""),
        lowest_sell_price=market_anchor,
        highest_purchase_price=None,
        on_sale_count=len(sale_book),
        raw={},
    )
    eligible_listed = [item for item in listed if _profit_ok(item, list_price, min_profit_ratio)]
    correct_listed = [
        item
        for item in eligible_listed
        if _same_price(own_prices.get(_commodity_id(item)), list_price, places)
    ]
    wrong_listed = [item for item in eligible_listed if item not in correct_listed]
    available = [
        item
        for item in items
        if _is_tradeable_now(item) and _profit_ok(item, list_price, min_profit_ratio)
    ]

    sort_key = lambda item: (
        getattr(item, "buy_price", None) or Decimal("999999999"),
        str(getattr(item, "asset_id", "")),
    )
    correct_listed.sort(key=sort_key)
    wrong_listed.sort(key=sort_key)
    available.sort(key=sort_key)
    selected = (correct_listed + wrong_listed + available)[:target_count]
    selected_ids = {str(getattr(item, "asset_id", "")) for item in selected}

    actions: list[ExitAction] = []
    for item in listed:
        current_price = own_prices.get(_commodity_id(item))
        rule = _action_rule(min_profit_ratio, price_minus, target_count, current_price)
        buy_price = getattr(item, "buy_price", None)
        if str(getattr(item, "asset_id", "")) not in selected_ids:
            reason = (
                f"{STRATEGY_ID}: delist excess/unprofitable listing; active={len(listed)}, "
                f"target={target_count}, buy={buy_price}, target_list={list_price}"
            )
            actions.append(ExitAction("delist_sale", item, list_price, None, snapshot, rule, reason))
            continue
        if _same_price(current_price, list_price, places):
            continue
        profit_ratio = (list_price - buy_price) / buy_price
        reason = (
            f"{STRATEGY_ID}: reprice active listing from {current_price or 'unknown'} to {list_price}; "
            f"competitor_lowest={market_anchor}, profit_ratio={money(profit_ratio * Decimal('100'))}%"
        )
        actions.append(ExitAction("relist_sale", item, list_price, list_price, snapshot, rule, reason))

    for item in available:
        if str(getattr(item, "asset_id", "")) not in selected_ids:
            continue
        buy_price = getattr(item, "buy_price", None)
        profit_ratio = (list_price - buy_price) / buy_price
        rule = _action_rule(min_profit_ratio, price_minus, target_count, None)
        reason = (
            f"{STRATEGY_ID}: fill listing deficit to {target_count}; buy={buy_price}, "
            f"competitor_lowest={market_anchor}, list={list_price}, "
            f"profit_ratio={money(profit_ratio * Decimal('100'))}%"
        )
        actions.append(ExitAction("list_sale", item, list_price, list_price, snapshot, rule, reason))
    return actions


def build_exit_actions(client: Any, inventory: list[Any], config: dict[str, Any], context: dict[str, Any]) -> list[ExitAction]:
    spec = _spec(context)
    max_total = max(
        0,
        int(
            spec.get(
                "max_reconcile_actions_per_cycle",
                spec.get("max_total_items_per_cycle", 0),
            )
        ),
    )
    remaining = max(0, int(context.get("remaining_actions", max_total or 0)))
    total_limit_candidates = [value for value in (max_total, remaining) if value > 0]
    total_limit = min(total_limit_candidates) if total_limit_candidates else 0
    page_size = max(1, min(int(spec.get("sale_book_page_size", 100)), 100))
    max_pages = max(1, int(spec.get("sale_book_max_pages", 20)))
    fetch_sale_book = context.get("fetch_template_sale_book")
    if not callable(fetch_sale_book):
        raise RuntimeError("exit plugin context missing fetch_template_sale_book")

    groups: dict[str, list[Any]] = {}
    for item in inventory:
        if _is_target_item(item, spec):
            groups.setdefault(_category_key(item), []).append(item)

    actions: list[ExitAction] = []
    plugin_log = context.get("log")
    for key in sorted(groups):
        items = groups[key]
        template_id = getattr(items[0], "template_id", None)
        if template_id in {None, ""}:
            continue
        try:
            sale_book = fetch_sale_book(
                client,
                int(template_id),
                page_size=page_size,
                max_pages=max_pages,
            )
        except Exception as exc:
            if callable(plugin_log):
                plugin_log(f"[EXIT BOOK ERROR] template={template_id} | {exc}")
            continue

        category_actions = build_category_reconciliation_actions(items, sale_book, spec)
        if total_limit > 0 and len(actions) + len(category_actions) > total_limit:
            if callable(plugin_log):
                plugin_log(
                    f"[EXIT RECONCILE DEFER] template={template_id} | "
                    f"actions={len(category_actions)} exceeds remaining={total_limit - len(actions)}"
                )
            continue
        actions.extend(category_actions)
    return actions
