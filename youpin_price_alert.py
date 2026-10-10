from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import youpin_daily_report as daily_report
import youpin_api as api
import youpin_major_bidder as bidder
import youpin_market_data as market_data


DEFAULT_POLL_SECONDS = 2700
DEFAULT_STATE_FILE = "state/price_alert_state.json"
DEFAULT_SUBJECT_PREFIX = "YouPin price alert"
BID_TARGET_KEYS = ("bid_target_price", "highest_bid_target_price", "bid_price_alert")
SALE_TARGET_KEYS = ("sale_target_price", "sell_target_price", "lowest_sell_target_price", "sale_price_alert")
LEGACY_TARGET_KEYS = ("target_price", "expected_price", "price")


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def price_alert_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("price_alert", {})
    if not isinstance(raw, dict):
        raw = {}
    report = config.get("daily_report", {})
    if not isinstance(report, dict):
        report = {}
    rules = raw.get("rules")
    if not isinstance(rules, list):
        rules = []
    return {
        "enabled": bool(raw.get("enabled", False)),
        "email_to": str(raw.get("email_to", report.get("email_to", "")) or "").strip(),
        "poll_seconds": max(60, int(raw.get("poll_seconds", DEFAULT_POLL_SECONDS))),
        "state_file": str(raw.get("state_file", DEFAULT_STATE_FILE)),
        "subject_prefix": str(raw.get("subject_prefix", DEFAULT_SUBJECT_PREFIX)),
        "rules": [rule for rule in rules if isinstance(rule, dict)],
    }


def ensure_alert_config(
    config: dict[str, Any],
    *,
    enabled: bool | None = None,
    email_to: str | None = None,
    poll_seconds: int | None = None,
) -> dict[str, Any]:
    alert = config.setdefault("price_alert", {})
    if not isinstance(alert, dict):
        alert = {}
        config["price_alert"] = alert
    if enabled is not None:
        alert["enabled"] = bool(enabled)
    else:
        alert.setdefault("enabled", False)
    if email_to is not None:
        alert["email_to"] = str(email_to or "").strip()
    else:
        report = config.get("daily_report", {})
        fallback_email = report.get("email_to", "") if isinstance(report, dict) else ""
        alert["email_to"] = str(alert.get("email_to", fallback_email) or "").strip()
    alert["poll_seconds"] = max(60, int(poll_seconds if poll_seconds is not None else alert.get("poll_seconds", DEFAULT_POLL_SECONDS)))
    alert.setdefault("state_file", DEFAULT_STATE_FILE)
    alert.setdefault("subject_prefix", DEFAULT_SUBJECT_PREFIX)
    if not isinstance(alert.get("rules"), list):
        alert["rules"] = []
    return alert


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_text()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def normalized(value: Any) -> str:
    return api.normalize_text(str(value or ""))


def first_decimal(rule: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        value = api.to_decimal(rule.get(key))
        if value is not None and value > 0:
            return value
    return None


def bid_target(rule: dict[str, Any]) -> Decimal | None:
    value = first_decimal(rule, BID_TARGET_KEYS)
    if value is not None:
        return value
    trigger = str(rule.get("trigger", "bid_or_sale"))
    if trigger in {"bid", "bid_or_sale", "both"}:
        return first_decimal(rule, LEGACY_TARGET_KEYS)
    return None


def sale_target(rule: dict[str, Any]) -> Decimal | None:
    value = first_decimal(rule, SALE_TARGET_KEYS)
    if value is not None:
        return value
    trigger = str(rule.get("trigger", "bid_or_sale"))
    if trigger in {"sale", "sell", "bid_or_sale", "both"}:
        return first_decimal(rule, LEGACY_TARGET_KEYS)
    return None


def rule_targets(rule: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    return bid_target(rule), sale_target(rule)


def item_key(item: api.InventoryItem) -> str:
    return item.market_hash_name or item.name


def rule_identity(rule: dict[str, Any]) -> str:
    return str(rule.get("market_hash_name", rule.get("name", ""))).strip()


def rule_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for rule in price_alert_config(config)["rules"]:
        if not bool(rule.get("enabled", True)):
            continue
        key = rule_identity(rule)
        if key:
            result[key] = rule
    return result


def rule_for_item(config: dict[str, Any], item: api.InventoryItem) -> dict[str, Any] | None:
    return rule_map(config).get(item_key(item))


def parse_optional_price(value: Any, field_name: str) -> Decimal | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    price = api.to_decimal(raw)
    if price is None:
        raise api.YouPinError(f"{field_name}必须是数字。")
    if price <= 0:
        raise api.YouPinError(f"{field_name}必须大于 0。")
    return price


def display_price(value: Decimal | None) -> str:
    return str(value) if value is not None else "-"


def upsert_rules_for_items(
    config: dict[str, Any],
    items: list[api.InventoryItem],
    *,
    bid_target_price: Decimal | None,
    sale_target_price: Decimal | None,
    email_to: str = "",
    enabled: bool = True,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
) -> int:
    if bid_target_price is None and sale_target_price is None:
        raise api.YouPinError("至少填写一个预警价。")
    alert = ensure_alert_config(config, enabled=enabled, email_to=email_to, poll_seconds=poll_seconds)
    rules = alert["rules"]
    updated = 0
    for item in items:
        market_hash = item_key(item)
        existing = next((rule for rule in rules if rule_identity(rule) == market_hash), None)
        if existing is None:
            existing = {}
            rules.append(existing)
        existing.update(
            {
                "enabled": True,
                "template_id": item.template_id,
                "market_hash_name": market_hash,
                "name": item.name,
                "trigger": "split",
            }
        )
        for key in (*BID_TARGET_KEYS, *SALE_TARGET_KEYS, *LEGACY_TARGET_KEYS):
            existing.pop(key, None)
        if bid_target_price is not None:
            existing["bid_target_price"] = float(bid_target_price)
        if sale_target_price is not None:
            existing["sale_target_price"] = float(sale_target_price)
        updated += 1
    return updated


def delete_rules_for_items(config: dict[str, Any], items: list[api.InventoryItem]) -> int:
    keys = {item_key(item) for item in items}
    if not keys:
        return 0
    alert = ensure_alert_config(config)
    rules = alert["rules"]
    before = len(rules)
    alert["rules"] = [rule for rule in rules if rule_identity(rule) not in keys]
    return before - len(alert["rules"])


def rule_key(rule: dict[str, Any], side: str, target: Decimal | None) -> str:
    identity = str(rule.get("market_hash_name") or rule.get("name") or rule.get("template_id") or "").strip()
    return f"{identity}|{side}|{target}"


def rule_matches_item(rule: dict[str, Any], item: api.InventoryItem) -> bool:
    template_id = rule.get("template_id")
    if template_id not in (None, ""):
        try:
            if item.template_id is not None and int(template_id) == int(item.template_id):
                return True
        except (TypeError, ValueError):
            pass
    market_hash = normalized(rule.get("market_hash_name"))
    name = normalized(rule.get("name"))
    item_hash = normalized(item.market_hash_name)
    item_name = normalized(item.name)
    return bool(
        (market_hash and market_hash in {item_hash, item_name})
        or (name and name in {item_name, item_hash})
    )


def money_text(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value.quantize(Decimal('0.01'))}"


def evaluate_rule(
    rule: dict[str, Any],
    items: list[api.InventoryItem],
    snapshot: api.MarketSnapshot,
) -> list[dict[str, Any]]:
    highest_bid = snapshot.highest_purchase_price
    lowest_sell = snapshot.lowest_sell_price
    tradable_count = sum(1 for item in items if item.status_text == "可交易")
    base = {
        "name": items[0].name if items else snapshot.name,
        "market_hash_name": items[0].market_hash_name if items else snapshot.market_hash_name,
        "template_id": snapshot.template_id,
        "highest_bid": money_text(highest_bid),
        "lowest_sell": money_text(lowest_sell),
        "inventory_count": len(items),
        "tradable_count": tradable_count,
    }
    alerts: list[dict[str, Any]] = []
    target = bid_target(rule)
    if target is not None and highest_bid is not None and highest_bid >= target:
        alerts.append(
            {
                **base,
                "key": rule_key(rule, "bid", target),
                "side": "求购预警",
                "target_price": money_text(target),
                "reason": f"最高求购 {money_text(highest_bid)} >= 求购预警价 {money_text(target)}",
            }
        )
    target = sale_target(rule)
    if target is not None and lowest_sell is not None and lowest_sell >= target:
        alerts.append(
            {
                **base,
                "key": rule_key(rule, "sale", target),
                "side": "在售预警",
                "target_price": money_text(target),
                "reason": f"卖一 {money_text(lowest_sell)} >= 在售预警价 {money_text(target)}",
            }
        )
    return alerts


def format_email_body(alerts: list[dict[str, Any]], checked_at: str) -> str:
    lines = [
        "悠悠有品库存价格预警",
        "",
        f"检查时间：{checked_at}",
        f"达标规则：{len(alerts)}",
        "",
    ]
    for idx, alert in enumerate(alerts, start=1):
        lines.extend(
            [
                f"{idx:02d}. {alert.get('name')}",
                f"类型：{alert.get('side')}    预警价：{alert.get('target_price')}",
                f"最高求购：{alert.get('highest_bid')}    卖一：{alert.get('lowest_sell')}",
                f"库存：{alert.get('inventory_count')}    可交易：{alert.get('tradable_count')}",
                f"触发原因：{alert.get('reason', '')}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def check_price_alerts(client: api.YouPinClient, config: dict[str, Any]) -> dict[str, Any]:
    alert = price_alert_config(config)
    checked_at = now_text()
    status: dict[str, Any] = {
        "enabled": alert["enabled"],
        "email_to": alert["email_to"],
        "checked_at": checked_at,
        "rule_count": len(alert["rules"]),
        "triggered_count": 0,
        "sent": False,
    }
    if not alert["enabled"]:
        return {**status, "reason": "disabled"}
    if not alert["rules"]:
        return {**status, "reason": "no_rules"}

    state_path = Path(alert["state_file"])
    state = load_state(state_path)
    sent_alerts = state.setdefault("sent_alerts", {})
    if not isinstance(sent_alerts, dict):
        sent_alerts = {}
        state["sent_alerts"] = sent_alerts

    inventory_state = bidder.load_state(config)
    inventory = market_data.fetch_inventory_items(client, config, state=inventory_state)
    grouped_items: dict[str, list[api.InventoryItem]] = {}
    for item in inventory:
        grouped_items.setdefault(item.market_hash_name or item.name, []).append(item)

    triggered: list[dict[str, Any]] = []
    active_keys: set[str] = set()
    for rule in alert["rules"]:
        if not bool(rule.get("enabled", True)):
            continue
        if bid_target(rule) is None and sale_target(rule) is None:
            continue
        matched_items = [item for item in inventory if rule_matches_item(rule, item)]
        if not matched_items:
            continue
        representative = matched_items[0]
        snapshot = market_data.build_market_snapshot(client, representative)
        evaluated = evaluate_rule(rule, matched_items, snapshot)
        possible_keys: set[str] = set()
        target = bid_target(rule)
        if target is not None:
            possible_keys.add(rule_key(rule, "bid", target))
        target = sale_target(rule)
        if target is not None:
            possible_keys.add(rule_key(rule, "sale", target))
        evaluated_keys = {item["key"] for item in evaluated}
        for key in possible_keys - evaluated_keys:
            sent_alerts.pop(key, None)
        if not evaluated:
            continue
        active_keys.update(evaluated_keys)
        triggered.extend(evaluated)

    for old_key in list(sent_alerts):
        if old_key not in active_keys:
            sent_alerts.pop(old_key, None)

    new_alerts = [item for item in triggered if item["key"] not in sent_alerts]
    status["triggered_count"] = len(triggered)
    status["new_triggered_count"] = len(new_alerts)
    status["triggered"] = triggered

    if not new_alerts:
        write_state(state_path, state)
        return {**status, "reason": "no_new_alerts"}
    if not alert["email_to"]:
        write_state(state_path, state)
        return {**status, "reason": "email_to_empty"}

    subject = f"{alert['subject_prefix']} {len(new_alerts)} item(s)"
    email_status = daily_report.send_text_email(
        to_addr=alert["email_to"],
        subject=subject,
        body=format_email_body(new_alerts, checked_at),
        config=config,
    )
    for item in new_alerts:
        sent_alerts[item["key"]] = {
            "sent_at": checked_at,
            "side": item.get("side"),
            "target_price": item.get("target_price"),
            "highest_bid": item.get("highest_bid"),
            "lowest_sell": item.get("lowest_sell"),
        }
    state["last_email_sent_at"] = checked_at
    state["last_email_to"] = alert["email_to"]
    write_state(state_path, state)
    return {**status, **email_status, "sent": True}
