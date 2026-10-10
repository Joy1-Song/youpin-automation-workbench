from __future__ import annotations

import youpin_major_bidder as bidder
import youpin_strategy_registry as strategy_registry


EXIT_MODE_TO_VALUE = {
    "求购达标优先": "bid_or_sale",
    "只丢求购": "bid_only",
    "只上架": "sale_only",
}
EXIT_VALUE_TO_MODE = {value: label for label, value in EXIT_MODE_TO_VALUE.items()}
EXIT_ACTION_LABELS = {
    "drop_to_bid": "丢求购",
    "list_sale": "上架",
    "relist_sale": "下架重挂",
    "delist_sale": "下架",
    "wait_cd": "等CD",
    "hold": "等待",
    "skip": "跳过",
}

SELECTION_MODE_TO_VALUE = {label: value for value, label in bidder.SELECTION_STRATEGY_LABELS.items()}
SELECTION_VALUE_TO_MODE = dict(bidder.SELECTION_STRATEGY_LABELS)
BID_MODE_TO_VALUE = {label: value for value, label in bidder.BID_STRATEGY_LABELS.items()}
BID_VALUE_TO_MODE = dict(bidder.BID_STRATEGY_LABELS)
CREATE_CHANNEL_TO_VALUE = {
    "API direct": "api",
    "APP command queue": "emulator_queue",
}
CREATE_VALUE_TO_CHANNEL = {value: label for label, value in CREATE_CHANNEL_TO_VALUE.items()}

BUILTIN_STRATEGY_PROFILES = {
    "低价优先 + 顺位求购": {
        "selection": "cheapest",
        "bid": "rank_chase",
        "quantity": "equal_budget",
        "create_channel": "emulator_queue",
        "allow_create": True,
        "note": "按卖一从低到高选品，并追第一/第二顺位求购。",
    },
    "动态稀缺 + 顺位求购": {
        "selection": "dynamic_scarcity",
        "bid": "rank_chase",
        "quantity": "equal_budget",
        "create_channel": "emulator_queue",
        "allow_create": True,
        "note": "结合在售量、求购量和低价区间评分，适合找更容易被资金推动的品类。",
    },
    "潜力评分 + 顺位求购": {
        "selection": "potential",
        "bid": "rank_chase",
        "quantity": "equal_budget",
        "create_channel": "emulator_queue",
        "allow_create": True,
        "note": "偏向 Steam 参考价、求购热度、稀缺度综合评分。",
    },
    "观察池优先": {
        "selection": "watchlist",
        "bid": "rank_chase",
        "quantity": "equal_budget",
        "create_channel": "emulator_queue",
        "allow_create": True,
        "note": "优先处理配置里的 watchlist，剩余名额按 fallback 策略补足。",
    },
}


def build_strategy_profiles(config: dict | None = None) -> dict[str, dict]:
    profiles = strategy_registry.discover_desktop_profiles(config)
    profiles.update(BUILTIN_STRATEGY_PROFILES)
    return profiles


def register_strategy_profile_modes(profiles: dict[str, dict]) -> None:
    for label, profile in profiles.items():
        selection = str(profile.get("selection", ""))
        bid = str(profile.get("bid", ""))
        if selection.startswith("plugin:"):
            SELECTION_MODE_TO_VALUE[label] = selection
            SELECTION_VALUE_TO_MODE[selection] = label
        if bid.startswith("plugin:"):
            BID_MODE_TO_VALUE[label] = bid
            BID_VALUE_TO_MODE[bid] = label
