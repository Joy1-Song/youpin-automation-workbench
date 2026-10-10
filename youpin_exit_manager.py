from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any

import youpin_builtin_strategies as strategy_engine
from youpin_strategy_sdk import money, normalize_text, to_decimal

VALID_ACTION_MODES = {"bid_or_sale", "bid_only", "sale_only"}
TARGET_KEYS = ("expected_price", "target_price", "price")


def strategy_config(config: dict[str, Any]) -> dict[str, Any]:
    strategy = config.setdefault("exit_strategy", {})
    if not isinstance(strategy, dict):
        strategy = {}
        config["exit_strategy"] = strategy
    strategy.setdefault("enabled", True)
    strategy.setdefault("auto_execute", False)
    strategy.setdefault("max_actions_per_cycle", 5)
    strategy.setdefault("round_places", 1)
    strategy.setdefault("tick", 0.1)
    strategy.setdefault("list_price_mode", "target")
    if not isinstance(strategy.get("rules"), list):
        strategy["rules"] = []
    if not isinstance(strategy.get("plugins"), list):
        strategy["plugins"] = []
    return strategy


def item_key(item: Any) -> str:
    return str(getattr(item, "market_hash_name", "") or getattr(item, "name", "") or "").strip()


def rule_identity(rule: dict[str, Any]) -> str:
    return str(rule.get("market_hash_name", rule.get("name", ""))).strip()


def rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    strategy = strategy_config(config)
    if not strategy.get("enabled", True):
        return []
    return [rule for rule in strategy["rules"] if isinstance(rule, dict) and rule.get("enabled", True)]


def plugin_specs(config: dict[str, Any]) -> list[dict[str, Any]]:
    strategy = strategy_config(config)
    if not strategy.get("enabled", True):
        return []

    specs: list[dict[str, Any]] = []
    direct_mode = str(strategy.get("mode", "")).strip()
    if direct_mode.startswith("plugin:"):
        specs.append({"mode": direct_mode, "enabled": True})

    for raw in strategy.get("plugins", []):
        if isinstance(raw, str):
            spec = {"mode": raw, "enabled": True}
        elif isinstance(raw, dict):
            spec = dict(raw)
        else:
            continue
        mode = str(spec.get("mode", spec.get("plugin", ""))).strip()
        if not mode:
            continue
        if not mode.startswith("plugin:"):
            mode = f"plugin:{mode}"
        spec["mode"] = mode
        if spec.get("enabled", True):
            specs.append(spec)
    return specs


def load_plugin_builder(spec: dict[str, Any], config: dict[str, Any]):
    return strategy_engine.load_plugin_callable(str(spec["mode"]), config, "build_exit_actions")


def rule_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for rule in rules(config):
        key = rule_identity(rule)
        if key:
            result[key] = rule
    return result


def rule_for_item(config: dict[str, Any], item: Any) -> dict[str, Any] | None:
    return rule_map(config).get(item_key(item))


def matches_rule(item: Any, rule: dict[str, Any]) -> bool:
    market_hash = normalize_text(str(rule.get("market_hash_name", "")))
    name = normalize_text(str(rule.get("name", "")))
    pattern = normalize_text(str(rule.get("pattern", "")))
    item_hash = normalize_text(str(getattr(item, "market_hash_name", "")))
    item_name = normalize_text(str(getattr(item, "name", "")))
    if market_hash and item_hash == market_hash:
        return True
    if name and item_name == name:
        return True
    if pattern and (pattern in item_hash or pattern in item_name):
        return True
    return False


def rule_target(rule: dict[str, Any]) -> Decimal | None:
    for key in TARGET_KEYS:
        value = to_decimal(rule.get(key))
        if value is not None and value > 0:
            return value
    return None


def rule_action_mode(rule: dict[str, Any]) -> str:
    mode = str(rule.get("action_mode", rule.get("action", "bid_or_sale"))).strip()
    if mode not in VALID_ACTION_MODES:
        return "bid_or_sale"
    return mode


def action_price(value: Decimal, config: dict[str, Any]) -> Decimal:
    strategy = strategy_config(config)
    places = int(strategy.get("round_places", 1))
    return money(value, places=places, rounding=ROUND_DOWN)


def sale_action_price(target: Decimal, lowest_sell: Decimal, config: dict[str, Any]) -> Decimal:
    strategy = strategy_config(config)
    tick = Decimal(str(strategy.get("tick", "0.1")))
    price_mode = str(strategy.get("list_price_mode", "target"))
    price = target
    if price_mode == "lowest_minus_tick":
        price = max(target, lowest_sell - tick)
    elif price_mode == "lowest":
        price = lowest_sell
    return action_price(price, config)


def upsert_rules_for_items(
    config: dict[str, Any],
    items: list[Any],
    *,
    target_price: Decimal,
    max_quantity: int,
    action_mode: str,
) -> int:
    if target_price <= 0:
        raise ValueError("target_price must be positive")
    if max_quantity <= 0:
        raise ValueError("max_quantity must be positive")
    action_mode = rule_action_mode({"action_mode": action_mode})
    strategy = strategy_config(config)
    rules_list = strategy["rules"]
    updated = 0
    for item in items:
        key = item_key(item)
        existing = next((rule for rule in rules_list if rule_identity(rule) == key), None)
        if existing is None:
            existing = {}
            rules_list.append(existing)
        existing.update(
            {
                "enabled": True,
                "market_hash_name": key,
                "name": getattr(item, "name", key),
                "expected_price": float(target_price),
                "max_quantity": int(max_quantity),
                "action_mode": action_mode,
            }
        )
        updated += 1
    strategy["default_action_mode"] = action_mode
    return updated


def delete_rules_for_items(config: dict[str, Any], items: list[Any]) -> int:
    keys = {item_key(item) for item in items}
    if not keys:
        return 0
    strategy = strategy_config(config)
    before = len(strategy["rules"])
    strategy["rules"] = [rule for rule in strategy["rules"] if rule_identity(rule) not in keys]
    return before - len(strategy["rules"])
