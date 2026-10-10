from __future__ import annotations

import importlib.util
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable

from youpin_strategy_sdk import (
    extract_highest_purchase_price,
    extract_subject,
    filter_tradeable_candidates,
    money,
    normalize_text,
    quantity_budget,
    round_down_money,
    strategy_key,
    target_rank_queue_price,
    target_rank_purchase_price,
    template_metric,
)


class StrategyError(RuntimeError):
    pass


SelectionStrategy = Callable[[list[Any], dict[str, Any]], list[Any]]
BidStrategy = Callable[[Any, Any, dict[str, Any], Any | None], tuple[Decimal | None, str]]
QuantityStrategy = Callable[[list[Decimal], dict[str, Any]], list[int]]


SELECTION_STRATEGY_LABELS = {
    "cheapest": "低价优先",
    "potential": "潜力评分",
    "dynamic_scarcity": "动态稀缺",
    "watchlist": "观察池优先",
}

BID_STRATEGY_LABELS = {
    "rank_chase": "追求购顺位",
    "lowest_sell_minus": "卖一减价",
    "lowest_sell_ratio": "卖一比例",
    "highest_purchase_plus": "求购一加价",
}

QUANTITY_STRATEGY_LABELS = {
    "fixed": "固定数量",
    "equal_budget": "预算均分",
}


def selection_strategy_label(mode: str) -> str:
    if mode.startswith("plugin:"):
        return f"插件策略 {mode.split(':', 1)[1]}"
    return SELECTION_STRATEGY_LABELS.get(mode, mode)


def bid_strategy_label(mode: str) -> str:
    if mode.startswith("plugin:"):
        return f"插件定价 {mode.split(':', 1)[1]}"
    return BID_STRATEGY_LABELS.get(mode, mode)


def select_cheapest(candidates: list[Any], config: dict[str, Any]) -> list[Any]:
    selection = config.get("selection_strategy", {})
    filtered = filter_tradeable_candidates(candidates, min_on_sale=int(selection.get("min_on_sale_count", 0)))
    for item in filtered:
        item.raw["_selection_score"] = 0
        item.raw["_selection_reason"] = "低价优先：按卖一价格从低到高排序"
    return sorted(filtered, key=lambda item: (item.lowest_sell_price or Decimal("999999"), item.name))


def select_watchlist(candidates: list[Any], config: dict[str, Any]) -> list[Any]:
    selection = config.get("selection_strategy", {})
    watchlist = selection.get("watchlist", [])
    filtered = filter_tradeable_candidates(candidates, min_on_sale=int(selection.get("min_on_sale_count", 0)))
    if not watchlist:
        return select_cheapest(filtered, config)

    priority: dict[str, int] = {}
    for index, raw_key in enumerate(watchlist):
        key = normalize_text(str(raw_key))
        if key:
            priority[key] = index

    selected: list[Any] = []
    remaining: list[Any] = []
    for item in filtered:
        matched_priority = min((priority[key] for key in strategy_key(item) if key in priority), default=None)
        if matched_priority is None:
            remaining.append(item)
            continue
        item.raw["_selection_score"] = float(10000 - matched_priority)
        item.raw["_selection_reason"] = f"观察池优先：命中第 {matched_priority + 1} 个观察项"
        selected.append(item)

    selected.sort(key=lambda item: (-Decimal(str(item.raw.get("_selection_score", 0))), item.lowest_sell_price or Decimal("999999"), item.name))
    if selection.get("fill_remaining", True):
        fallback_config = dict(config)
        fallback_selection = dict(selection)
        fallback_mode = str(selection.get("fallback_mode", "cheapest"))
        if fallback_mode == "watchlist":
            fallback_mode = "cheapest"
        fallback_selection["mode"] = fallback_mode
        fallback_config["selection_strategy"] = fallback_selection
        selected.extend([item for item in apply_selection_strategy(remaining, fallback_config) if item not in selected])
    return selected


def select_scored_candidates(candidates: list[Any], config: dict[str, Any], mode: str) -> list[Any]:
    selection = config.get("selection_strategy", {})
    filtered = filter_tradeable_candidates(candidates, min_on_sale=int(selection.get("min_on_sale_count", 0)))
    max_candidates = int(selection.get("max_candidates", 120))
    filtered = sorted(filtered, key=lambda item: (item.lowest_sell_price or Decimal("999999"), item.name))[:max_candidates]
    if not filtered:
        return []

    weights = selection.get("weights", {})
    if mode == "dynamic_scarcity":
        scarcity_weight = Decimal(str(weights.get("scarcity", 35)))
        demand_supply_weight = Decimal(str(weights.get("demand_supply", 30)))
        affordability_weight = Decimal(str(weights.get("affordability", 20)))
        steam_weight = Decimal(str(weights.get("steam_discount", 10)))
        brand_weight = Decimal(str(weights.get("brand", 5)))
    else:
        steam_weight = Decimal(str(weights.get("steam_discount", 45)))
        demand_weight = Decimal(str(weights.get("purchase_demand", 25)))
        scarcity_weight = Decimal(str(weights.get("scarcity", 20)))
        affordability_weight = Decimal(str(weights.get("affordability", 10)))
        demand_supply_weight = Decimal("0")
        brand_weight = Decimal("0")

    prices = [template_metric(item, "price") for item in filtered]
    steam_ratios: list[Decimal] = []
    demands: list[Decimal] = []
    supplies: list[Decimal] = []
    demand_supply_ratios: list[Decimal] = []
    for item in filtered:
        price = template_metric(item, "price")
        steam = template_metric(item, "steam_price")
        ratio = Decimal("0")
        if price > 0 and steam > 0:
            ratio = min(steam / price, Decimal("3"))
        demand = template_metric(item, "purchase_count")
        supply = template_metric(item, "on_sale")
        steam_ratios.append(ratio)
        demands.append(demand)
        supplies.append(supply)
        demand_supply_ratios.append(demand / max(supply, Decimal("1")))

    max_ratio = max(steam_ratios) or Decimal("1")
    max_demand = max(demands) or Decimal("1")
    max_supply = max(supplies) or Decimal("1")
    max_demand_supply = max(demand_supply_ratios) or Decimal("1")
    max_price = max(prices) or Decimal("1")
    min_price = min(prices) or Decimal("0")
    price_span = max(max_price - min_price, Decimal("1"))
    brand_teams = {normalize_text(team) for team in selection.get("brand_teams", [])}

    scored: list[Any] = []
    for item, ratio, demand, supply, ds_ratio, price in zip(filtered, steam_ratios, demands, supplies, demand_supply_ratios, prices):
        ratio_score = ratio / max_ratio
        demand_score = demand / max_demand
        scarcity_score = Decimal("1") - (supply / max_supply)
        demand_supply_score = ds_ratio / max_demand_supply
        affordability_score = Decimal("1") - ((price - min_price) / price_span)
        subject = extract_subject(item.name, str(config["finish"]), str(config["event"])) or ""
        brand_score = Decimal("1") if normalize_text(subject) in brand_teams else Decimal("0")
        if mode == "dynamic_scarcity":
            score = (
                scarcity_score * scarcity_weight
                + demand_supply_score * demand_supply_weight
                + affordability_score * affordability_weight
                + ratio_score * steam_weight
                + brand_score * brand_weight
            )
        else:
            score = (
                ratio_score * steam_weight
                + demand_score * demand_weight
                + scarcity_score * scarcity_weight
                + affordability_score * affordability_weight
            )
        item.raw["_selection_score"] = float(score)
        item.raw["_selection_reason"] = (
            f"score={money(score)} steam_ratio={money(ratio)} "
            f"demand={int(demand)} on_sale={int(supply)} demand/supply={money(ds_ratio)}"
        )
        scored.append(item)

    return sorted(scored, key=lambda item: (-Decimal(str(item.raw.get("_selection_score", 0))), item.lowest_sell_price or Decimal("999999"), item.name))


SELECTION_STRATEGIES: dict[str, SelectionStrategy] = {
    "cheapest": select_cheapest,
    "potential": lambda candidates, config: select_scored_candidates(candidates, config, "potential"),
    "dynamic_scarcity": lambda candidates, config: select_scored_candidates(candidates, config, "dynamic_scarcity"),
    "watchlist": select_watchlist,
}


def load_plugin_callable(mode: str, config: dict[str, Any], function_name: str) -> Callable[..., Any]:
    plugin_name = mode.split(":", 1)[1].strip()
    if not plugin_name:
        raise StrategyError(f"插件策略名不能为空，例如 plugin:my_strategy")
    plugin_config = config.get("strategy_plugins", {})
    directories = plugin_config.get("directories", ["strategies"])
    for directory in directories:
        path = Path(str(directory)) / f"{plugin_name}.py"
        if not path.exists():
            continue
        spec = importlib.util.spec_from_file_location(f"youpin_strategy_{plugin_name}", path)
        if spec is None or spec.loader is None:
            raise StrategyError(f"无法加载策略插件：{path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        func = getattr(module, function_name, None)
        if not callable(func):
            raise StrategyError(f"策略插件 {path} 必须定义 {function_name}")
        return func
    raise StrategyError(f"找不到策略插件 {plugin_name}.py，搜索目录：{directories}")


def apply_selection_strategy(candidates: list[Any], config: dict[str, Any]) -> list[Any]:
    mode = str(config.get("selection_strategy", {}).get("mode", "potential"))
    if mode.startswith("plugin:"):
        selected = load_plugin_callable(mode, config, "select_candidates")(candidates, config)
        return filter_tradeable_candidates(selected)
    strategy = SELECTION_STRATEGIES.get(mode)
    if strategy is None:
        known = ", ".join(sorted(SELECTION_STRATEGIES))
        raise StrategyError(f"未知选品策略：{mode}。内置策略：{known}；插件策略格式：plugin:文件名")
    return strategy(candidates, config)


def calculate_bid_price(client: Any, template: Any, config: dict[str, Any], existing_order: Any | None = None) -> tuple[Decimal | None, str]:
    strategy = config.get("bid_strategy", {})
    mode = str(strategy.get("mode", "lowest_sell_minus"))
    if mode.startswith("plugin:"):
        return load_plugin_callable(mode, config, "calculate_bid_price")(client, template, config, existing_order)

    places = int(strategy.get("round_places", 1))
    min_unit_price = Decimal(str(strategy.get("min_unit_price", "0.1")))
    max_unit_price = Decimal(str(strategy.get("max_unit_price", "0")))
    reference = template.lowest_sell_price

    if reference is None or reference <= 0:
        if strategy.get("skip_if_no_reference_price", True):
            return None, "没有参考卖盘价"
        reference = min_unit_price

    if mode == "lowest_sell_minus":
        price = reference - Decimal(str(strategy.get("minus", "0.1")))
    elif mode == "lowest_sell_ratio":
        price = reference * Decimal(str(strategy.get("ratio", "1.0")))
    elif mode == "highest_purchase_plus":
        tick = Decimal(str(strategy.get("tick", "0.1")))
        payload = client.get_template_purchase_orders(template.template_id)
        highest = extract_highest_purchase_price(payload)
        if highest is None:
            price = reference - Decimal(str(strategy.get("minus", "0.01")))
        else:
            price = min(highest + tick, reference)
    elif mode == "rank_chase":
        entry_ratio = Decimal(str(strategy.get("entry_ratio", strategy.get("ratio", "0.85"))))
        target_rank = int(strategy.get("target_rank", 2))
        step_up = Decimal(str(strategy.get("step_up", "0.5")))
        step_down = Decimal(str(strategy.get("step_down", "0.1")))
        tick = Decimal(str(strategy.get("tick", "0.1")))
        max_bid_to_sell_ratio = Decimal(str(strategy.get("max_bid_to_sell_ratio", "0.95")))
        cap = reference * max_bid_to_sell_ratio
        live_book_price = None
        if strategy.get("use_live_order_book", True):
            payload = client.get_template_purchase_orders(template.template_id)
            rank_target_mode = str(strategy.get("rank_target_mode", "price_level")).lower()
            rank_price_func = target_rank_queue_price if rank_target_mode in {"queue", "position", "order"} else target_rank_purchase_price
            live_book_price = rank_price_func(
                payload,
                target_rank,
                tick,
                exclude_order_no=str(getattr(existing_order, "order_no", "") or ""),
            )
        if live_book_price is not None:
            price = min(live_book_price, cap)
            mode = f"rank_chase_live_book_{strategy.get('rank_target_mode', 'price_level')}_target_{target_rank}_{live_book_price}"
        elif existing_order is None or existing_order.purchase_price is None:
            price = reference * entry_ratio
            mode = f"rank_chase_entry_{entry_ratio}"
        else:
            current = existing_order.purchase_price
            rank = existing_order.rank
            book_top = existing_order.max_purchase_price if strategy.get("use_book_top_price", True) else None
            if book_top is not None and book_top > 0:
                if target_rank <= 1:
                    price = book_top + tick
                else:
                    price = book_top - tick
                price = min(price, cap)
                mode = f"rank_chase_book_top_{book_top}_target_{target_rank}_rank_{rank}"
            elif rank is not None and rank > target_rank:
                price = current + step_up
                mode = f"rank_chase_up_rank_{rank}_to_{target_rank}"
            elif rank == 1 and target_rank >= 2:
                price = current - step_down
                mode = "rank_chase_trim_rank_1"
            else:
                price = current
                mode = f"rank_chase_keep_rank_{rank}"
            price = min(price, cap)
    else:
        raise StrategyError(f"未知价格策略：{mode}")

    price = round_down_money(price, places=places)
    if price < min_unit_price:
        return None, f"计算价 {price} 低于 min_unit_price {min_unit_price}"
    if max_unit_price > 0 and price > max_unit_price:
        return None, f"计算价 {price} 超过 max_unit_price {max_unit_price}"
    return price, mode


def allocate_quantities(prices: list[Decimal], config: dict[str, Any]) -> list[int]:
    quantity_strategy = config.get("quantity_strategy", {})
    mode = str(quantity_strategy.get("mode", "equal_budget"))
    if mode.startswith("plugin:"):
        return load_plugin_callable(mode, config, "allocate_quantities")(prices, config)
    if mode == "fixed":
        return [max(0, int(config.get("purchase_num", 1))) for _ in prices]
    if mode != "equal_budget":
        known = ", ".join(sorted(QUANTITY_STRATEGY_LABELS))
        raise StrategyError(f"未知数量策略：{mode}。内置策略：{known}")

    budget = quantity_budget(config)
    if budget <= 0:
        raise StrategyError("数量策略预算必须大于 0。")

    min_qty = max(0, int(quantity_strategy.get("min_quantity_per_item", 1)))
    max_qty = max(min_qty, int(quantity_strategy.get("max_quantity_per_item", 999999)))
    qtys = [0 for _ in prices]
    remaining = budget

    for idx, price in enumerate(prices):
        if price <= 0:
            continue
        cost = price * Decimal(min_qty)
        if min_qty > 0 and remaining >= cost:
            qtys[idx] = min_qty
            remaining -= cost

    target_per_item = budget / Decimal(max(len(prices), 1))
    for idx, price in enumerate(prices):
        if price <= 0:
            continue
        target_qty = min(max_qty, int((target_per_item / price).to_integral_value(rounding=ROUND_DOWN)))
        while qtys[idx] < target_qty:
            if qtys[idx] == 0 and min_qty > 1:
                cost = price * Decimal(min_qty)
                if remaining < cost:
                    break
                qtys[idx] = min_qty
                remaining -= cost
                continue
            if remaining < price:
                break
            qtys[idx] += 1
            remaining -= price

    while True:
        changed = False
        for idx, price in enumerate(prices):
            if price <= 0 or qtys[idx] >= max_qty:
                continue
            if qtys[idx] == 0 and min_qty > 1:
                cost = price * Decimal(min_qty)
                if remaining < cost:
                    continue
                qtys[idx] = min_qty
                remaining -= cost
                changed = True
                continue
            if remaining < price:
                continue
            qtys[idx] += 1
            remaining -= price
            changed = True
        if not changed:
            break

    return qtys
