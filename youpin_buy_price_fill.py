from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import youpin_execution_bridge as execution_bridge


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_text(value: str) -> str:
    return " ".join(str(value).strip().lower().replace("\u2019", "'").split())


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    text = str(value).strip().replace(",", "")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if not match:
            return None
        try:
            return Decimal(match.group(0))
        except (InvalidOperation, ValueError):
            return None


def pick(source: dict[str, Any], keys: list[str]) -> Any:
    lowered = {key.lower(): key for key in source}
    for key in keys:
        actual = lowered.get(key.lower())
        if actual is not None:
            return source[actual]
    return None


def parse_youpin_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromtimestamp(int(text) / (1000 if len(text) >= 13 else 1))
    except (TypeError, ValueError, OSError):
        return None


def buy_price_fill_config(config: dict[str, Any]) -> dict[str, Any]:
    fill = config.get("buy_price_fill", {})
    if not isinstance(fill, dict):
        fill = {}
    keywords_value = fill.get("keywords", [])
    if not isinstance(keywords_value, list):
        keywords_value = []
    return {
        "enabled": bool(fill.get("enabled", True)),
        "lookback_hours": max(1, int(fill.get("lookback_hours", 48))),
        "match_window_minutes": max(1, int(fill.get("match_window_minutes", 180))),
        "max_pending_events": max(10, int(fill.get("max_pending_events", 500))),
        "emulator_auto_queue": bool(fill.get("emulator_auto_queue", False)),
        "emulator_auto_process": bool(fill.get("emulator_auto_process", False)),
        "emulator_auto_submit": bool(fill.get("emulator_auto_submit", False)),
        "emulator_process_limit": max(1, int(fill.get("emulator_process_limit", 3))),
        "stickers_only": bool(fill.get("stickers_only", False)),
        "keywords": [str(keyword).strip() for keyword in keywords_value if str(keyword).strip()],
        "finish": str(fill.get("finish", "") or "").strip(),
    }


def buy_price_fill_item_allowed(item: Any, config: dict[str, Any]) -> bool:
    fill = buy_price_fill_config(config)
    text = normalize_text(f"{getattr(item, 'name', '')} {getattr(item, 'market_hash_name', '')}")
    if fill["stickers_only"] and not any(marker in text for marker in ("sticker", "\u5370\u82b1")):
        return False

    keywords = [normalize_text(keyword) for keyword in fill["keywords"] if normalize_text(keyword)]
    if keywords and not any(keyword in text for keyword in keywords):
        return False

    finish = normalize_text(fill["finish"])
    if finish:
        finish_markers = ("holo", "\u5168\u606f") if finish == "holo" else (finish,)
        if not any(marker in text for marker in finish_markers):
            return False
    return True


def order_snapshot_from_action(action: dict[str, Any]) -> dict[str, Any] | None:
    order_no = str(action.get("existing_order_no") or "").strip()
    if not order_no:
        return None
    template_id = action.get("template_id")
    try:
        parsed_template_id = int(template_id)
    except (TypeError, ValueError):
        return None
    purchase_price = to_decimal(action.get("bid_price") or action.get("existing_purchase_price"))
    try:
        purchase_num = int(action.get("purchase_num") or 0)
    except (TypeError, ValueError):
        purchase_num = 0
    if purchase_price is None or purchase_num <= 0:
        return None
    return {
        "order_no": order_no,
        "template_id": parsed_template_id,
        "name": str(action.get("name") or ""),
        "purchase_price": str(purchase_price),
        "purchase_num": purchase_num,
        "seen_at": now_text(),
    }


def order_snapshots_from_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    explicit = state.get("last_seen_purchase_orders")
    if isinstance(explicit, list):
        for item in explicit:
            if not isinstance(item, dict):
                continue
            order_no = str(item.get("order_no") or "").strip()
            if order_no:
                result[order_no] = dict(item)
    if result:
        return result

    actions = state.get("actions")
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            snapshot = order_snapshot_from_action(action)
            if snapshot is not None:
                result[str(snapshot["order_no"])] = snapshot
    return result


def order_snapshots_from_orders(orders: dict[int, Any]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for order in orders.values():
        if not getattr(order, "order_no", "") or getattr(order, "purchase_price", None) is None or not getattr(order, "purchase_num", None):
            continue
        snapshots.append(
            {
                "order_no": order.order_no,
                "template_id": order.template_id,
                "name": order.commodity_name,
                "purchase_price": str(order.purchase_price),
                "purchase_num": int(order.purchase_num),
                "seen_at": now_text(),
            }
        )
    return snapshots


def trim_purchase_price_fill_events(events: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    fill = buy_price_fill_config(config)
    cutoff = datetime.now() - timedelta(hours=int(fill["lookback_hours"]))
    kept: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        detected_at = parse_youpin_datetime(event.get("detected_at"))
        if detected_at is not None and detected_at < cutoff:
            continue
        kept.append(event)
    return kept[-int(fill["max_pending_events"]) :]


def update_purchase_price_fill_events(
    state: dict[str, Any],
    current_orders: dict[int, Any],
    config: dict[str, Any],
) -> None:
    fill = buy_price_fill_config(config)
    if not fill["enabled"]:
        state["last_seen_purchase_orders"] = order_snapshots_from_orders(current_orders)
        return

    previous = order_snapshots_from_state(state)
    current_by_order_no = {
        order.order_no: order
        for order in current_orders.values()
        if getattr(order, "order_no", "")
    }
    events = state.get("purchase_price_fill_events", [])
    if not isinstance(events, list):
        events = []
    events = trim_purchase_price_fill_events(events, config)
    existing_keys = {str(event.get("event_key") or "") for event in events if isinstance(event, dict)}

    for order_no, old in previous.items():
        try:
            old_qty = int(old.get("purchase_num") or 0)
        except (TypeError, ValueError):
            old_qty = 0
        old_price = to_decimal(old.get("purchase_price"))
        if old_qty <= 0 or old_price is None:
            continue
        current = current_by_order_no.get(order_no)
        if current is None:
            continue
        current_qty = int(current.purchase_num or 0) if current and current.purchase_num is not None else 0
        filled_qty = old_qty - current_qty
        if filled_qty <= 0:
            continue
        template_id = int(old.get("template_id") or (current.template_id if current else 0))
        detected_at = now_text()
        event_key = f"{order_no}:{template_id}:{old_price}:{filled_qty}:{detected_at}"
        if event_key in existing_keys:
            continue
        events.append(
            {
                "event_key": event_key,
                "source": "purchase_order_fill",
                "order_no": order_no,
                "template_id": template_id,
                "name": str(old.get("name") or (current.commodity_name if current else "")),
                "buy_price": str(old_price),
                "quantity": filled_qty,
                "matched_quantity": 0,
                "detected_at": detected_at,
            }
        )
        existing_keys.add(event_key)

    state["purchase_price_fill_events"] = trim_purchase_price_fill_events(events, config)
    state["last_seen_purchase_orders"] = order_snapshots_from_orders(current_orders)


def inventory_add_time(item: Any) -> datetime | None:
    raw = getattr(item, "raw", {}) or {}
    return parse_youpin_datetime(
        pick(
            raw,
            [
                "AssetAddTime",
                "assetAddTime",
                "AddTime",
                "addTime",
                "CreateTime",
                "createTime",
                "PurchaseTime",
                "purchaseTime",
                "BuyTime",
                "buyTime",
            ],
        )
    )


def inventory_matches_fill_event(
    item: Any,
    event: dict[str, Any],
    config: dict[str, Any],
    previous_asset_ids: set[str] | None = None,
) -> bool:
    try:
        event_template_id = int(event.get("template_id") or 0)
    except (TypeError, ValueError):
        event_template_id = 0
    item_template_id = getattr(item, "template_id", None)
    if item_template_id and event_template_id and item_template_id == event_template_id:
        template_matches = True
    else:
        event_name = normalize_text(str(event.get("name") or ""))
        template_matches = bool(event_name and event_name == normalize_text(getattr(item, "name", "")))
    if not template_matches:
        return False

    asset_id = str(getattr(item, "asset_id", "") or "")
    asset_time = inventory_add_time(item)
    detected_at = parse_youpin_datetime(event.get("detected_at"))
    if asset_time is None and previous_asset_ids and asset_id and asset_id in previous_asset_ids:
        return False
    if asset_time is None or detected_at is None:
        return True
    window = timedelta(minutes=int(buy_price_fill_config(config)["match_window_minutes"]))
    return detected_at - window <= asset_time <= detected_at + window


def apply_inferred_buy_prices_to_inventory(
    inventory: list[Any],
    config: dict[str, Any],
    state: dict[str, Any],
) -> list[Any]:
    fill = buy_price_fill_config(config)
    if not fill["enabled"]:
        return inventory
    previous_asset_ids = {
        str(asset_id)
        for asset_id in state.get("last_inventory_asset_ids", [])
        if str(asset_id)
    }
    events = state.get("purchase_price_fill_events", [])
    if not isinstance(events, list) or not events:
        return inventory
    events = trim_purchase_price_fill_events(events, config)
    changed = False
    filled_inventory: list[Any] = []
    for item in inventory:
        if getattr(item, "buy_price", None) is not None:
            filled_inventory.append(item)
            continue
        matched_event: dict[str, Any] | None = None
        for event in events:
            if not isinstance(event, dict):
                continue
            try:
                quantity = int(event.get("quantity") or 0)
                matched_quantity = int(event.get("matched_quantity") or 0)
            except (TypeError, ValueError):
                continue
            if matched_quantity >= quantity:
                continue
            buy_price = to_decimal(event.get("buy_price"))
            if buy_price is None or not inventory_matches_fill_event(item, event, config, previous_asset_ids):
                continue
            matched_event = event
            break
        if matched_event is None:
            filled_inventory.append(item)
            continue
        buy_price = to_decimal(matched_event.get("buy_price"))
        if buy_price is None:
            filled_inventory.append(item)
            continue
        raw = dict(getattr(item, "raw", {}) or {})
        raw["_inferred_buy_price"] = str(buy_price)
        raw["_inferred_buy_price_source"] = matched_event.get("source", "purchase_order_fill")
        raw["_inferred_buy_price_order_no"] = matched_event.get("order_no", "")
        filled_inventory.append(replace(item, buy_price=buy_price, raw=raw))
        matched_event["matched_quantity"] = int(matched_event.get("matched_quantity") or 0) + 1
        matched_assets = matched_event.setdefault("matched_assets", [])
        asset_id = str(getattr(item, "asset_id", "") or "")
        if isinstance(matched_assets, list) and asset_id:
            matched_assets.append(asset_id)
        changed = True
    if changed:
        state["purchase_price_fill_events"] = events
        state.setdefault("purchase_price_fill_updated_at", now_text())
    state["last_inventory_asset_ids"] = [getattr(item, "asset_id", "") for item in inventory if getattr(item, "asset_id", "")]
    state["last_inventory_seen_at"] = now_text()
    return filled_inventory


def queue_inferred_buy_price_fill_commands(inventory: list[Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    fill = buy_price_fill_config(config)
    if not fill["enabled"] or not fill["emulator_auto_queue"]:
        return []
    inferred_items = [
        item
        for item in inventory
        if (getattr(item, "raw", {}) or {}).get("_inferred_buy_price") and buy_price_fill_item_allowed(item, config)
    ]
    if not inferred_items:
        return []
    execution_config = config.get("execution", {})
    if not isinstance(execution_config, dict):
        execution_config = {}
    command_file = str(execution_config.get("emulator_command_file", execution_bridge.DEFAULT_COMMAND_FILE))
    return execution_bridge.queue_buy_price_fill_commands(inferred_items, command_file, source="buy_price_fill")


def pending_fill_quantity(state: dict[str, Any]) -> int:
    events = state.get("purchase_price_fill_events", [])
    pending_count = 0
    if not isinstance(events, list):
        return pending_count
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            quantity = int(event.get("quantity") or 0)
            matched_quantity = int(event.get("matched_quantity") or 0)
        except (TypeError, ValueError):
            continue
        pending_count += max(quantity - matched_quantity, 0)
    return pending_count


def refresh_status(
    orders: dict[int, Any],
    inventory: list[Any],
    queued_commands: list[dict[str, Any]],
    state: dict[str, Any],
) -> dict[str, Any]:
    return {
        "enabled": True,
        "checked_at": now_text(),
        "order_count": len(orders),
        "inventory_count": len(inventory),
        "inferred_buy_price_count": sum(1 for item in inventory if (getattr(item, "raw", {}) or {}).get("_inferred_buy_price")),
        "missing_buy_price_count": sum(1 for item in inventory if getattr(item, "buy_price", None) is None),
        "pending_fill_quantity": pending_fill_quantity(state),
        "queued_buy_price_fill_commands": len(queued_commands),
    }
