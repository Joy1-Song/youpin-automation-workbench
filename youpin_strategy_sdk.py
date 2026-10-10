from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any

from youpin_api import deep_records, money, normalize_text, parse_count_text, pick, to_decimal


def extract_subject(name: str, finish: str, event: str) -> str | None:
    prefixes = ["Sticker | ", "印花 | "]
    prefix = next((item for item in prefixes if name.startswith(item)), None)
    if prefix is None:
        return None
    suffixes = [
        f" ({finish}) | {event}",
        f"（{finish}）| {event}",
        f"（{finish}） | {event}",
    ]
    suffix = next((item for item in suffixes if name.endswith(item)), None)
    if suffix is None:
        return None
    return name[len(prefix) : -len(suffix)].strip()


def template_metric(template: Any, key: str, default: Decimal = Decimal("0")) -> Decimal:
    if key == "price":
        return template.lowest_sell_price or default
    if key == "steam_price":
        return to_decimal(pick(template.raw, ["SteamPrice", "steamPrice"])) or default
    if key == "on_sale":
        value = template.on_sale_count
        return Decimal(value) if value is not None else default
    if key == "purchase_count":
        return Decimal(parse_count_text(pick(template.raw, ["templatePurchaseCountText", "TemplatePurchaseCountText"])))
    return default


def strategy_key(template: Any) -> set[str]:
    return {
        str(template.template_id),
        normalize_text(template.name),
        normalize_text(template.hash_name),
    }


def filter_tradeable_candidates(candidates: list[Any], min_on_sale: int = 0) -> list[Any]:
    return [
        item
        for item in candidates
        if item.lowest_sell_price is not None
        and item.lowest_sell_price > 0
        and (item.on_sale_count or 0) >= min_on_sale
    ]


def extract_highest_purchase_price(payload: dict[str, Any]) -> Decimal | None:
    prices = extract_purchase_order_prices(payload)
    if not prices:
        return None
    return max(prices)


def extract_purchase_order_prices(payload: dict[str, Any], exclude_order_no: str = "") -> list[Decimal]:
    prices: list[Decimal] = []
    for record in deep_records(payload):
        order_no = pick(record, ["purchaseNo", "PurchaseNo", "orderNo", "OrderNo", "orderId", "OrderId"])
        if exclude_order_no and str(order_no or "") == str(exclude_order_no):
            continue
        parsed = to_decimal(pick(record, ["purchasePrice", "PurchasePrice", "unitPrice", "UnitPrice", "price", "Price"]))
        if parsed is not None and parsed > 0:
            prices.append(parsed)
    return sorted(prices, reverse=True)


def target_rank_purchase_price(
    payload: dict[str, Any],
    target_rank: int,
    tick: Decimal,
    *,
    exclude_order_no: str = "",
) -> Decimal | None:
    prices = extract_purchase_order_prices(payload, exclude_order_no=exclude_order_no)
    if not prices:
        return None
    top = prices[0]
    if target_rank <= 1:
        return top + tick
    return top - (tick * Decimal(target_rank - 1))


def target_rank_queue_price(
    payload: dict[str, Any],
    target_rank: int,
    tick: Decimal,
    *,
    exclude_order_no: str = "",
) -> Decimal | None:
    prices = extract_purchase_order_prices(payload, exclude_order_no=exclude_order_no)
    if not prices:
        return None
    if target_rank <= 1:
        return prices[0] + tick
    index = min(max(target_rank - 2, 0), len(prices) - 1)
    return prices[index]


def quantity_budget(config: dict[str, Any]) -> Decimal:
    strategy = config.get("bid_strategy", {})
    quantity_strategy = config.get("quantity_strategy", {})
    budget = quantity_strategy.get("budget", strategy.get("max_cycle_spend", strategy.get("max_daily_spend", "0")))
    return Decimal(str(budget))


def round_down_money(value: Decimal, places: int) -> Decimal:
    return money(value, places=places, rounding=ROUND_DOWN)
