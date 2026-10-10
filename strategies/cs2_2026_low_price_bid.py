from __future__ import annotations

import copy
from decimal import Decimal, ROUND_DOWN

import youpin_builtin_strategies as builtin


STRATEGY_ID = "cs2_2026_low_price_bid"
STRATEGY_NAME = "26低价贴纸求购策略"
STRATEGY_VERSION = "1.0.0"
PLATFORM_MIN_PURCHASE_QTY = 3
DESCRIPTION = (
    "当前项目的基准策略：搜索 Cologne 2026 战队全息贴纸，"
    "只取卖一最便宜的 10 个品类，按目标求购顺位追价，"
    "单品数量限制在 5-7 张以内，并用 1000 元作为 24 小时成交额上限。"
)


DEFAULT_CONFIG = {
    "event": "Cologne 2026",
    "event_aliases": ["Cologne 2026", "2026年科隆锦标赛"],
    "finish": "Holo",
    "finish_aliases": ["Holo", "全息"],
    "keywords": ["Cologne 2026", "2026年科隆锦标赛"],
    "top_n": 10,
    "refreshes_per_day": 12,
    "selection_strategy": {
        "mode": f"plugin:{STRATEGY_ID}",
        "min_on_sale_count": 5,
        "max_candidates": 120,
    },
    "execution": {
        "create_order_channel": "emulator_queue",
        "emulator_command_file": "state/emulator_commands.json",
        "emulator_status_file": "state/emulator_status.json",
        "emulator_auto_process_create_orders": True,
        "emulator_auto_submit_create_orders": True,
        "emulator_create_process_limit": 10,
    },
    "bid_strategy": {
        "mode": f"plugin:{STRATEGY_ID}",
        "entry_ratio": 0.85,
        "target_rank": 2,
        "rank_target_mode": "queue",
        "use_live_order_book": True,
        "use_book_top_price": True,
        "modify_interval_seconds": 60,
        "allow_create_orders": False,
        "step_up": 0.5,
        "step_down": 0.1,
        "max_bid_to_sell_ratio": 1.0,
        "prevent_bid_at_or_above_sell": True,
        "tick": 0.1,
        "min_unit_price": 0.1,
        "max_unit_price": 1000.0,
        "max_cycle_spend": 1000.0,
        "max_daily_spend": 1000.0,
        "round_places": 1,
        "skip_if_no_reference_price": True,
    },
    "quantity_strategy": {
        "mode": f"plugin:{STRATEGY_ID}",
        "budget": 1000.0,
        "min_quantity_per_item": PLATFORM_MIN_PURCHASE_QTY,
        "max_quantity_per_item": 5,
    },
    "purchase_risk": {
        "enabled": True,
        "rolling_success_limit": 1000.0,
        "lookback_hours": 24,
        "window_mode": "calendar_day",
        "day_start_hour": 0,
        "include_active_order_exposure": False,
        "active_order_exposure_multiplier": 2.0,
        "fail_if_inventory_unavailable": True,
    },
    "buy_price_fill": {
        "enabled": True,
        "lookback_hours": 48,
        "match_window_minutes": 180,
        "max_pending_events": 500,
        "emulator_auto_queue": False,
        "emulator_auto_process": False,
        "emulator_auto_submit": False,
        "emulator_process_limit": 3,
    },
    "cleanup_strategy": {
        "enabled": True,
        "auto_execute": True,
        "scope": "strategy_targets_only",
        "max_delete_per_cycle": 20,
        "reset_target_orders_each_cycle": True,
        "reset_only_partially_filled": False,
        "event": "Cologne 2026",
        "finish": "Holo",
        "preserve_template_ids": [],
        "preserve_order_nos": [],
        "preserve_keywords": [],
    },
    "cs_update_guard": {
        "enabled": True,
        "auto_cancel": True,
        "pause_bidding_on_update": True,
        "appid": 730,
        "poll_seconds": 60,
        "news_count": 10,
        "maxlength": 500,
        "state_file": "state/cs_update_guard_state.json",
        "title_keywords": ["Counter-Strike 2 Update", "Release Notes"],
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
    "min_quantity_per_item": PLATFORM_MIN_PURCHASE_QTY,
    "max_quantity_per_item": 5,
    "max_quantity_hard_limit": 7,
    "active_order_exposure_multiplier": 2,
    "top_n": 10,
    "note": "只求购最便宜 10 个 26 全息战队贴纸，单品 5-7 张，1000 元作为 24h 成交上限。",
}


def _config_with_mode(config, section, mode):
    patched = copy.deepcopy(config)
    patched.setdefault(section, {})
    patched[section]["mode"] = mode
    return patched


def _round_down(value: Decimal, places: int) -> Decimal:
    quantum = Decimal("1").scaleb(-places)
    return value.quantize(quantum, rounding=ROUND_DOWN)


def _apply_sell_side_cap(price: Decimal, template, config) -> tuple[Decimal, str | None]:
    strategy = config.get("bid_strategy", {})
    if not bool(strategy.get("prevent_bid_at_or_above_sell", True)):
        return price, None
    reference = getattr(template, "lowest_sell_price", None)
    if reference is None or reference <= 0:
        return price, None

    places = int(strategy.get("round_places", 1))
    tick = Decimal(str(strategy.get("tick", "0.1")))
    max_ratio = Decimal(str(strategy.get("max_bid_to_sell_ratio", "0.95")))
    cap = reference - tick
    if max_ratio > 0:
        cap = min(cap, reference * max_ratio)
    cap = _round_down(cap, places)
    if cap <= 0 or price <= cap:
        return price, None
    return cap, f"sell_side_cap_{cap}"


def select_candidates(candidates, config):
    """选品：只取卖一最便宜的 10 个品类。"""
    patched = _config_with_mode(config, "selection_strategy", "cheapest")
    selected = builtin.select_cheapest(candidates, patched)[:10]
    for item in selected:
        item.raw["_selection_reason"] = (
            f"{STRATEGY_NAME}：低价优先，卖一={item.lowest_sell_price}，在售={item.on_sale_count or 0}"
        )
    return selected


def calculate_bid_price(client, template, config, existing_order):
    """定价：当前逻辑等价于 rank_chase，围绕目标求购顺位自动调价。"""
    patched = _config_with_mode(config, "bid_strategy", "rank_chase")
    price, reason = builtin.calculate_bid_price(client, template, patched, existing_order)
    if price is None:
        return None, f"{STRATEGY_NAME}：{reason}"
    price, cap_reason = _apply_sell_side_cap(price, template, patched)
    if cap_reason:
        reason = f"{reason}; {cap_reason}"
    return price, f"{STRATEGY_NAME}：{reason}"


def allocate_quantities(prices: list[Decimal], config):
    """数量：预算均分，单品最多 7 张，默认 5 张。"""
    patched = _config_with_mode(config, "quantity_strategy", "equal_budget")
    quantity = patched.setdefault("quantity_strategy", {})
    min_qty = int(quantity.get("min_quantity_per_item", PLATFORM_MIN_PURCHASE_QTY))
    max_qty = int(quantity.get("max_quantity_per_item", 5))
    if min_qty > 7:
        raise builtin.StrategyError("min_quantity_per_item cannot exceed this strategy's hard limit of 7")
    min_qty = max(min_qty, PLATFORM_MIN_PURCHASE_QTY)
    quantity["min_quantity_per_item"] = min_qty
    quantity["max_quantity_per_item"] = min(max(max_qty, min_qty), 7)
    return builtin.allocate_quantities(prices, patched)


def apply_defaults(config):
    """可选辅助：把本策略默认配置合并到传入配置中。"""
    patched = copy.deepcopy(config)
    for key, value in DEFAULT_CONFIG.items():
        if isinstance(value, dict):
            target = patched.setdefault(key, {})
            for child_key, child_value in value.items():
                target.setdefault(child_key, child_value)
        else:
            patched.setdefault(key, value)
    return patched
