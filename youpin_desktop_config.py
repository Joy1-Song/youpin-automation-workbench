from __future__ import annotations

from pathlib import Path
from typing import Any

import youpin_major_bidder as bidder
import youpin_strategy_registry as strategy_registry


def load_initial_config(path: Path) -> dict[str, Any]:
    load_path = path
    if not load_path.exists() and Path("config.example.json").exists():
        load_path = Path("config.example.json")
    try:
        return bidder.load_config(load_path)
    except Exception:
        return {}


def detect_strategy_profile(config: dict[str, Any], profiles: dict[str, dict]) -> str:
    selection = str(config.get("selection_strategy", {}).get("mode", "cheapest"))
    bid = str(config.get("bid_strategy", {}).get("mode", "rank_chase"))
    quantity = str(config.get("quantity_strategy", {}).get("mode", "equal_budget"))
    for label, profile in profiles.items():
        if (
            selection == str(profile.get("selection"))
            and bid == str(profile.get("bid"))
            and quantity == str(profile.get("quantity"))
        ):
            return label
    if selection == "dynamic_scarcity":
        return "动态稀缺 + 顺位求购"
    if selection == "potential":
        return "潜力评分 + 顺位求购"
    if selection == "watchlist":
        return "观察池优先"
    return "低价优先 + 顺位求购"


def merge_defaults(config: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    return strategy_registry.merge_defaults(config, defaults)
