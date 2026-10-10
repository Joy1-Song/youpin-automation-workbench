from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import youpin_builtin_strategies as strategy_engine
import youpin_buy_price_fill as buy_price_fill_engine
import youpin_cs_update_alert as cs_update_alert_engine
import youpin_config as config_engine
import youpin_exit_manager as exit_engine
import youpin_execution_bridge as execution_bridge
import youpin_order_reconciler as order_reconciler
import youpin_risk_manager as risk_engine
from youpin_api import (
    CleanupAction,
    CyclePlan,
    ExistingPurchaseOrder,
    ExitAction,
    InventoryItem,
    MarketSnapshot,
    PlanAction,
    PurchaseRiskSnapshot,
    StickerTemplate,
    YouPinClient,
    YouPinError,
    deep_records,
    money,
    normalize_text,
    parse_count_text,
    pick,
    response_code,
    response_msg,
    to_decimal,
)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    print(f"[{now_text()}] {message}", flush=True)


def normalize_template(record: dict[str, Any]) -> StickerTemplate | None:
    template_id = pick(record, ["templateId", "TemplateId", "Id", "id"])
    name = pick(record, ["commodityName", "CommodityName", "name", "Name", "templateName", "TemplateName"])
    hash_name = pick(record, ["templateHashName", "TemplateHashName", "marketHashName", "MarketHashName", "hashName", "HashName"])
    price = pick(record, ["price", "Price", "sellMinPrice", "SellMinPrice", "minPrice", "MinPrice"])
    on_sale_count = pick(record, ["onSaleCount", "OnSaleCount", "sellNum", "SellNum", "commodityNum", "CommodityNum"])

    if template_id is None or not name:
        return None

    try:
        parsed_template_id = int(template_id)
    except (TypeError, ValueError):
        return None

    parsed_price = to_decimal(price)
    parsed_count: int | None
    try:
        parsed_count = int(on_sale_count) if on_sale_count is not None else None
    except (TypeError, ValueError):
        parsed_count = None

    return StickerTemplate(
        template_id=parsed_template_id,
        name=str(name),
        hash_name=str(hash_name or name),
        lowest_sell_price=parsed_price,
        on_sale_count=parsed_count,
        raw=record,
    )


def extract_subject(name: str, finish: str, event: str) -> str | None:
    return strategy_engine.extract_subject(name, finish, event)


def _configured_aliases(config: dict[str, Any], key: str, aliases_key: str) -> list[str]:
    values = [config.get(key), *(config.get(aliases_key, []) or [])]
    return list(dict.fromkeys(str(value).strip() for value in values if str(value or "").strip()))


def is_target_team_sticker(template: StickerTemplate, config: dict[str, Any]) -> bool:
    team_names = {normalize_text(team) for team in config.get("team_names", [])}
    events = _configured_aliases(config, "event", "event_aliases")
    finishes = _configured_aliases(config, "finish", "finish_aliases")
    for event in events:
        for finish in finishes:
            subject = extract_subject(template.name, finish, event)
            if subject and normalize_text(subject) in team_names:
                return True
    return False


def template_metric(template: StickerTemplate, key: str, default: Decimal = Decimal("0")) -> Decimal:
    return strategy_engine.template_metric(template, key, default)


SelectionStrategy = strategy_engine.SelectionStrategy

SELECTION_STRATEGY_LABELS = strategy_engine.SELECTION_STRATEGY_LABELS
BID_STRATEGY_LABELS = strategy_engine.BID_STRATEGY_LABELS


def selection_strategy_label(mode: str) -> str:
    return strategy_engine.selection_strategy_label(mode)


def bid_strategy_label(mode: str) -> str:
    return strategy_engine.bid_strategy_label(mode)


def strategy_key(template: StickerTemplate) -> set[str]:
    return strategy_engine.strategy_key(template)


def filter_tradeable_candidates(candidates: list[StickerTemplate], min_on_sale: int = 0) -> list[StickerTemplate]:
    return strategy_engine.filter_tradeable_candidates(candidates, min_on_sale=min_on_sale)


def select_cheapest(candidates: list[StickerTemplate], config: dict[str, Any]) -> list[StickerTemplate]:
    return strategy_engine.select_cheapest(candidates, config)


def select_watchlist(candidates: list[StickerTemplate], config: dict[str, Any]) -> list[StickerTemplate]:
    return strategy_engine.select_watchlist(candidates, config)


def select_scored_candidates(candidates: list[StickerTemplate], config: dict[str, Any], mode: str) -> list[StickerTemplate]:
    if mode not in {"potential", "dynamic_scarcity"}:
        raise YouPinError(f"unknown scored selection mode: {mode}")
    return strategy_engine.select_scored_candidates(candidates, config, mode)


def select_potential(candidates: list[StickerTemplate], config: dict[str, Any]) -> list[StickerTemplate]:
    return select_scored_candidates(candidates, config, "potential")


def select_dynamic_scarcity(candidates: list[StickerTemplate], config: dict[str, Any]) -> list[StickerTemplate]:
    return select_scored_candidates(candidates, config, "dynamic_scarcity")


SELECTION_STRATEGIES = strategy_engine.SELECTION_STRATEGIES


def load_selection_strategy_plugin(mode: str, config: dict[str, Any]) -> SelectionStrategy:
    try:
        return strategy_engine.load_plugin_callable(mode, config, "select_candidates")
    except strategy_engine.StrategyError as exc:
        raise YouPinError(str(exc)) from exc


def apply_selection_scores(candidates: list[StickerTemplate], config: dict[str, Any]) -> list[StickerTemplate]:
    try:
        return strategy_engine.apply_selection_strategy(candidates, config)
    except strategy_engine.StrategyError as exc:
        raise YouPinError(str(exc)) from exc


def extract_lowest_sell_detail(payload: dict[str, Any]) -> Decimal | None:
    for record in deep_records(payload):
        price = pick(record, ["price", "Price", "sellPrice", "SellPrice"])
        parsed = to_decimal(price)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def extract_highest_purchase_price(payload: dict[str, Any]) -> Decimal | None:
    return strategy_engine.extract_highest_purchase_price(payload)


def normalize_existing_order(record: dict[str, Any]) -> ExistingPurchaseOrder | None:
    template_id = pick(record, ["templateId", "TemplateId"])
    order_no = pick(record, ["orderNo", "OrderNo", "orderId", "OrderId"])
    name = pick(record, ["commodityName", "CommodityName", "templateName", "TemplateName", "name", "Name"])
    purchase_price = pick(record, ["purchasePrice", "PurchasePrice", "unitPrice", "UnitPrice", "price", "Price"])
    max_purchase_price = pick(record, ["maxPurchasePrice", "MaxPurchasePrice", "highestPurchasePrice", "HighestPurchasePrice"])
    purchase_num = pick(record, ["purchaseNum", "PurchaseNum", "quantity", "Quantity", "commodityNum", "CommodityNum"])
    supply_quantity = pick(record, ["supplyQuantity", "SupplyQuantity"])
    rank = pick(record, ["rank", "Rank"])

    if template_id is None or order_no is None:
        return None

    try:
        parsed_template_id = int(template_id)
    except (TypeError, ValueError):
        return None

    try:
        parsed_num = int(purchase_num) if purchase_num is not None else None
    except (TypeError, ValueError):
        parsed_num = None

    try:
        parsed_supply = int(supply_quantity) if supply_quantity is not None else 0
    except (TypeError, ValueError):
        parsed_supply = 0
    try:
        parsed_rank = int(rank) if rank not in {None, ""} else None
    except (TypeError, ValueError):
        parsed_rank = None

    return ExistingPurchaseOrder(
        order_no=str(order_no),
        template_id=parsed_template_id,
        commodity_name=str(name or ""),
        purchase_price=to_decimal(purchase_price),
        max_purchase_price=to_decimal(max_purchase_price),
        purchase_num=parsed_num,
        supply_quantity=parsed_supply,
        rank=parsed_rank,
        raw=record,
    )


def normalize_inventory_item(record: dict[str, Any]) -> InventoryItem | None:
    template_info = pick(record, ["TemplateInfo", "templateInfo"])
    if not isinstance(template_info, dict):
        template_info = {}

    asset_id = pick(record, ["steamAssetId", "SteamAssetId", "assetId", "AssetId", "id", "Id"])
    template_id = pick(record, ["templateId", "TemplateId"])
    if template_id is None:
        template_id = pick(template_info, ["Id", "id", "templateId", "TemplateId"])
    name = pick(record, ["name", "Name", "commodityName", "CommodityName", "templateName", "TemplateName"])
    if not name:
        name = pick(template_info, ["CommodityName", "commodityName", "Name", "name"])
    market_hash_name = pick(record, ["marketHashName", "MarketHashName", "templateHashName", "TemplateHashName"])
    if not market_hash_name:
        market_hash_name = pick(template_info, ["CommodityHashName", "commodityHashName", "MarketHashName", "marketHashName"])
    cooldown_desc = pick(record, ["cacheExpirationDesc", "CacheExpirationDesc", "cooldownDesc", "CooldownDesc"])
    if not cooldown_desc:
        cooldown_desc = pick(record, ["CoolingDesc", "coolingDesc"])
    asset_status = pick(record, ["assetStatus", "AssetStatus"])
    tradable = pick(record, ["Tradable", "tradable", "isTradable", "IsTradable"])
    buy_price = pick(
        record,
        [
            "assetBuyPrice",
            "AssetBuyPrice",
            "AssetBuyPrice",
            "updateAssetBuyPriceDesc",
            "UpdateAssetBuyPriceDesc",
            "buyPrice",
            "BuyPrice",
            "purchasePrice",
            "PurchasePrice",
            "costPrice",
            "CostPrice",
        ],
    )
    reference_price = pick(record, ["price", "Price", "referencePrice", "ReferencePrice"])
    if reference_price is None:
        reference_price = pick(template_info, ["MarkPrice", "markPrice", "Price", "price"])
    price_desc = pick(record, ["priceDesc", "PriceDesc"])
    if not price_desc:
        price_desc = pick(template_info, ["ShowMarkPrice", "showMarkPrice"])

    if asset_id is None and not name:
        return None

    try:
        parsed_template_id = int(template_id) if template_id not in {None, ""} else None
    except (TypeError, ValueError):
        parsed_template_id = None

    try:
        parsed_status = int(asset_status) if asset_status not in {None, ""} else None
    except (TypeError, ValueError):
        parsed_status = None
    if isinstance(tradable, bool):
        parsed_tradable = tradable
    elif tradable in {0, "0", "false", "False"}:
        parsed_tradable = False
    elif tradable in {1, "1", "true", "True"}:
        parsed_tradable = True
    else:
        parsed_tradable = None

    return InventoryItem(
        asset_id=str(asset_id or ""),
        template_id=parsed_template_id,
        name=str(name or market_hash_name or ""),
        market_hash_name=str(market_hash_name or ""),
        asset_status=parsed_status,
        tradable=parsed_tradable,
        cooldown_desc=str(cooldown_desc or "").strip(),
        buy_price=to_decimal(buy_price),
        reference_price=to_decimal(reference_price),
        price_desc=str(price_desc or ""),
        raw=record,
    )


def ensure_success(payload: dict[str, Any], action: str) -> None:
    code = response_code(payload)
    msg = response_msg(payload)
    if code not in {0, "0", None} and "成功" not in msg.lower():
        raise YouPinError(f"{action} 失败：code={code}, msg={msg}, response={payload}")


def ensure_delete_success(payload: dict[str, Any], action: str) -> None:
    ensure_success(payload, action)
    data = payload.get("data", payload.get("Data"))
    if isinstance(data, dict) and data.get("status") is False:
        biz_msg = data.get("bizMsg") or data.get("msg") or "接口返回 status=false"
        raise YouPinError(f"{action} 失败：{biz_msg}，response={payload}")


def ensure_sell_success(payload: dict[str, Any], action: str) -> None:
    code = response_code(payload)
    if code not in {0, "0", None}:
        msg = response_msg(payload)
        raise YouPinError(f"{action} 失败：code={code}, msg={msg}, response={payload}")
    data = payload.get("Data", payload.get("data"))
    if not isinstance(data, list):
        return
    failures = []
    for item in data:
        if not isinstance(item, dict):
            continue
        status = item.get("Status", item.get("status"))
        remark = str(item.get("Remark", item.get("remark", "")) or "")
        if status not in {1, "1", True}:
            if "审核中" in remark:
                continue
            failures.append(f"{item.get('AssetId', item.get('assetId', ''))}: {remark or '未知原因'}")
    if failures:
        raise YouPinError(f"{action} 部分失败：" + "；".join(failures))


def state_file_path(config: dict[str, Any]) -> Path:
    state_file = Path(config.get("state_file", "state/youpin_bidder_state.json"))
    if not state_file.is_absolute():
        state_file = Path.cwd() / state_file
    return state_file


def load_state(config: dict[str, Any]) -> dict[str, Any]:
    path = state_file_path(config)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    path = state_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def copy_config_for_budget(config: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(config)


def today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def state_daily_spend(state: dict[str, Any]) -> Decimal:
    budget = state.get("daily_budget", {})
    if not isinstance(budget, dict) or budget.get("date") != today_key():
        return Decimal("0")
    return to_decimal(budget.get("submitted_spend")) or Decimal("0")


def parse_state_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def buy_price_fill_config(config: dict[str, Any]) -> dict[str, Any]:
    return buy_price_fill_engine.buy_price_fill_config(config)


def buy_price_fill_item_allowed(item: InventoryItem, config: dict[str, Any]) -> bool:
    return buy_price_fill_engine.buy_price_fill_item_allowed(item, config)


def order_snapshot_from_action(action: dict[str, Any]) -> dict[str, Any] | None:
    return buy_price_fill_engine.order_snapshot_from_action(action)


def order_snapshots_from_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return buy_price_fill_engine.order_snapshots_from_state(state)


def order_snapshots_from_orders(orders: dict[int, ExistingPurchaseOrder]) -> list[dict[str, Any]]:
    return buy_price_fill_engine.order_snapshots_from_orders(orders)


def trim_purchase_price_fill_events(events: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    return buy_price_fill_engine.trim_purchase_price_fill_events(events, config)


def update_purchase_price_fill_events(
    state: dict[str, Any],
    current_orders: dict[int, ExistingPurchaseOrder],
    config: dict[str, Any],
) -> None:
    buy_price_fill_engine.update_purchase_price_fill_events(state, current_orders, config)


def inventory_matches_fill_event(
    item: InventoryItem,
    event: dict[str, Any],
    config: dict[str, Any],
    previous_asset_ids: set[str] | None = None,
) -> bool:
    return buy_price_fill_engine.inventory_matches_fill_event(item, event, config, previous_asset_ids)


def apply_inferred_buy_prices_to_inventory(
    inventory: list[InventoryItem],
    config: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> list[InventoryItem]:
    state_ref = state if state is not None else load_state(config)
    filled_inventory = buy_price_fill_engine.apply_inferred_buy_prices_to_inventory(inventory, config, state_ref)
    if state is None:
        write_state(config, state_ref)
    return filled_inventory


def queue_inferred_buy_price_fill_commands(inventory: list[InventoryItem], config: dict[str, Any]) -> list[dict[str, Any]]:
    return buy_price_fill_engine.queue_inferred_buy_price_fill_commands(inventory, config)


def refresh_buy_price_fill_state(client: YouPinClient, config: dict[str, Any]) -> dict[str, Any]:
    state = load_state(config)
    if not buy_price_fill_config(config)["enabled"]:
        return {"enabled": False}
    orders = fetch_existing_orders(client)
    update_purchase_price_fill_events(state, orders, config)
    inventory = fetch_inventory_items(client, config, state=state)
    queued_commands = queue_inferred_buy_price_fill_commands(inventory, config)
    write_state(config, state)
    return buy_price_fill_engine.refresh_status(orders, inventory, queued_commands, state)


def cs_update_guard_config(config: dict[str, Any]) -> dict[str, Any]:
    guard = config.get("cs_update_guard", {})
    if not isinstance(guard, dict):
        guard = {}
    keywords = guard.get("title_keywords", ["Counter-Strike 2 Update", "Release Notes", "Update"])
    if not isinstance(keywords, list):
        keywords = []
    alert = cs_update_alert_engine.alert_config(config, guard)
    return {
        "enabled": bool(guard.get("enabled", False)),
        "auto_cancel": bool(guard.get("auto_cancel", True)),
        "pause_bidding_on_update": bool(guard.get("pause_bidding_on_update", True)),
        "email_alert": bool(alert["enabled"]),
        "email_to": str(alert["email_to"]),
        "email_subject_prefix": str(alert["subject_prefix"]),
        "appid": int(guard.get("appid", 730)),
        "poll_seconds": max(15, int(guard.get("poll_seconds", 60))),
        "news_count": max(1, int(guard.get("news_count", 10))),
        "maxlength": max(0, int(guard.get("maxlength", 500))),
        "state_file": str(guard.get("state_file", "state/cs_update_guard_state.json")),
        "title_keywords": [str(item).lower() for item in keywords if str(item).strip()],
    }


def cs_update_guard_state_path(config: dict[str, Any]) -> Path:
    guard = cs_update_guard_config(config)
    path = Path(guard["state_file"])
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def load_cs_update_guard_state(config: dict[str, Any]) -> dict[str, Any]:
    path = cs_update_guard_state_path(config)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_cs_update_guard_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    path = cs_update_guard_state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_steam_app_news(guard: dict[str, Any]) -> list[dict[str, Any]]:
    url = (
        "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
        f"?appid={guard['appid']}&count={guard['news_count']}"
        f"&maxlength={guard['maxlength']}&format=json"
    )
    request = Request(url, headers={"User-Agent": "YouPin-CS-Update-Guard/1.0"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise YouPinError(f"CS update guard failed to read Steam news: {exc}") from exc
    newsitems = payload.get("appnews", {}).get("newsitems", [])
    return newsitems if isinstance(newsitems, list) else []


def latest_cs_update_news(config: dict[str, Any]) -> dict[str, Any] | None:
    guard = cs_update_guard_config(config)
    if not guard["enabled"]:
        return None
    candidates: list[dict[str, Any]] = []
    for item in fetch_steam_app_news(guard):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", ""))
        lower_title = title.lower()
        if guard["title_keywords"] and not any(keyword in lower_title for keyword in guard["title_keywords"]):
            continue
        try:
            date_value = int(item.get("date") or 0)
        except (TypeError, ValueError):
            date_value = 0
        candidates.append(
            {
                "gid": str(item.get("gid", "")),
                "date": date_value,
                "title": title,
                "url": str(item.get("url", "")),
                "feedlabel": str(item.get("feedlabel", "")),
            }
        )
    if not candidates:
        return None
    return max(candidates, key=lambda item: (int(item.get("date") or 0), str(item.get("gid", ""))))


def cancel_all_purchase_orders(client: YouPinClient, batch_size: int = 20) -> tuple[int, Decimal, list[ExistingPurchaseOrder]]:
    orders = list(fetch_existing_orders(client).values())
    order_nos = [order.order_no for order in orders if order.order_no]
    if not order_nos:
        return 0, Decimal("0"), orders
    for start in range(0, len(order_nos), max(1, batch_size)):
        chunk = order_nos[start : start + batch_size]
        response = client.delete_purchase_orders(chunk)
        ensure_delete_success(response, f"cancel {len(chunk)} purchase orders")
        time.sleep(0.25)
    spend = sum((order.spend for order in orders), Decimal("0"))
    return len(order_nos), spend, orders


def check_cs_update_guard(client: YouPinClient, config: dict[str, Any], live: bool) -> dict[str, Any]:
    guard = cs_update_guard_config(config)
    if not guard["enabled"]:
        return {"enabled": False}

    state = load_cs_update_guard_state(config)
    latest = latest_cs_update_news(config)
    checked_at = now_text()
    if latest is None:
        state.update({"enabled": True, "checked_at": checked_at, "last_error": "no matching Steam news"})
        save_cs_update_guard_state(config, state)
        return state

    latest_gid = str(latest.get("gid", ""))
    latest_date = int(latest.get("date") or 0)
    seen_gid = str(state.get("last_seen_gid", ""))
    try:
        seen_date = int(state.get("last_seen_date") or 0)
    except (TypeError, ValueError):
        seen_date = 0

    if not seen_gid and not seen_date:
        state.update(
            {
                "enabled": True,
                "armed": True,
                "halted": False,
                "checked_at": checked_at,
                "last_seen_gid": latest_gid,
                "last_seen_date": latest_date,
                "last_seen_title": latest.get("title", ""),
                "last_seen_url": latest.get("url", ""),
                "last_error": "",
            }
        )
        save_cs_update_guard_state(config, state)
        log(f"CS update guard armed at {latest.get('title', '')} ({latest_gid})")
        return state

    detected_update = latest_date > seen_date or (latest_date == seen_date and latest_gid and latest_gid != seen_gid)
    state.update(
        {
            "enabled": True,
            "armed": True,
            "checked_at": checked_at,
            "latest_gid": latest_gid,
            "latest_date": latest_date,
            "latest_title": latest.get("title", ""),
            "latest_url": latest.get("url", ""),
            "detected_update": detected_update,
            "last_error": "",
        }
    )
    if not detected_update:
        effective_live = bool(live and not config.get("dry_run", True))
        if state.get("halted") and guard["auto_cancel"] and effective_live:
            cancelled_count, cancelled_spend, _ = cancel_all_purchase_orders(client)
            if cancelled_count:
                log(f"CS update guard is halted; cancelled {cancelled_count} reopened purchase orders")
            state.update(
                {
                    "halt_cancelled_order_count": cancelled_count,
                    "halt_cancelled_order_spend": str(cancelled_spend),
                }
            )
        if state.get("halted") and str(state.get("trigger_gid", "")) == latest_gid and str(state.get("last_email_gid", "")) != latest_gid:
            try:
                state["email_alert"] = cs_update_alert_engine.send_alert(config, state, latest, state)
            except Exception as exc:
                state["email_alert"] = cs_update_alert_engine.failure_status(bool(guard["email_alert"]), exc)
                state["last_email_error"] = str(exc)
        save_cs_update_guard_state(config, state)
        return state

    effective_live = bool(live and not config.get("dry_run", True))
    cancelled_count = 0
    cancelled_spend = Decimal("0")
    if guard["auto_cancel"] and effective_live:
        cancelled_count, cancelled_spend, _ = cancel_all_purchase_orders(client)
        log(
            f"CS update detected: {latest.get('title', '')}; "
            f"cancelled {cancelled_count} purchase orders, spend={cancelled_spend}"
        )
    else:
        log(
            f"CS update detected: {latest.get('title', '')}; "
            f"auto cancel skipped because live={live}, dry_run={config.get('dry_run', True)}"
        )

    state.update(
        {
            "halted": bool(guard["pause_bidding_on_update"]),
            "last_triggered_at": checked_at,
            "trigger_gid": latest_gid,
            "trigger_date": latest_date,
            "trigger_title": latest.get("title", ""),
            "trigger_url": latest.get("url", ""),
            "cancelled_order_count": cancelled_count,
            "cancelled_order_spend": str(cancelled_spend),
            "effective_live": effective_live,
            "last_seen_gid": latest_gid,
            "last_seen_date": latest_date,
            "last_seen_title": latest.get("title", ""),
            "last_seen_url": latest.get("url", ""),
        }
    )
    try:
        state["email_alert"] = cs_update_alert_engine.send_alert(config, state, latest, state)
    except Exception as exc:
        state["email_alert"] = cs_update_alert_engine.failure_status(bool(guard["email_alert"]), exc)
        state["last_email_error"] = str(exc)
    save_cs_update_guard_state(config, state)
    return state


def parse_youpin_datetime(value: Any) -> datetime | None:
    return risk_engine.parse_youpin_datetime(value)


def inventory_add_time(item: InventoryItem) -> datetime | None:
    return risk_engine.inventory_add_time(item)


def purchase_event_from_inventory(item: InventoryItem) -> dict[str, Any] | None:
    return risk_engine.purchase_event_from_inventory(item)


def purchase_event_key(event: dict[str, Any]) -> str:
    return risk_engine.purchase_event_key(event)


def purchase_event_time(event: dict[str, Any]) -> datetime | None:
    return risk_engine.purchase_event_time(event)


def purchase_event_spend(event: dict[str, Any]) -> Decimal | None:
    return risk_engine.purchase_event_spend(event)


def purchase_events_from_inventory(inventory: list[InventoryItem]) -> list[dict[str, Any]]:
    return risk_engine.purchase_events_from_inventory(inventory)


def merge_recent_purchase_events(
    previous_events: list[dict[str, Any]],
    inventory_events: list[dict[str, Any]],
    *,
    lookback_hours: int = 24,
    now: datetime | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    return risk_engine.merge_recent_purchase_events(
        previous_events,
        inventory_events,
        lookback_hours=lookback_hours,
        now=now,
        since=since,
    )


def purchase_events_spend(events: list[dict[str, Any]]) -> tuple[Decimal, int]:
    return risk_engine.purchase_events_spend(events)


def recent_success_purchase_spend(
    inventory: list[InventoryItem],
    *,
    lookback_hours: int = 24,
    now: datetime | None = None,
) -> tuple[Decimal, int]:
    return risk_engine.recent_success_purchase_spend(inventory, lookback_hours=lookback_hours, now=now)


def purchase_risk_config(config: dict[str, Any]) -> dict[str, Any]:
    return risk_engine.purchase_risk_config(config)


def active_order_spend(orders: dict[int, ExistingPurchaseOrder]) -> Decimal:
    return risk_engine.active_order_spend(orders)


def active_order_spend_after_plan(
    actions: list[PlanAction],
    existing_orders: dict[int, ExistingPurchaseOrder],
    cleanup_actions: list[CleanupAction] | None = None,
) -> Decimal:
    return risk_engine.active_order_spend_after_plan(actions, existing_orders, cleanup_actions)


def build_purchase_risk_snapshot(
    client: YouPinClient,
    config: dict[str, Any],
    existing_orders: dict[int, ExistingPurchaseOrder] | None = None,
    actions: list[PlanAction] | None = None,
    cleanup_actions: list[CleanupAction] | None = None,
    state: dict[str, Any] | None = None,
) -> PurchaseRiskSnapshot:
    return risk_engine.build_purchase_risk_snapshot(
        client,
        config,
        existing_orders,
        actions,
        cleanup_actions,
        state,
        inventory_fetcher=lambda active_client, active_config, active_state: fetch_inventory_items(
            active_client,
            active_config,
            state=active_state,
        ),
        checked_at=now_text(),
    )


def actionable_spend(actions: list[PlanAction]) -> Decimal:
    return risk_engine.actionable_spend(actions)


def action_new_exposure(action: PlanAction) -> Decimal:
    return risk_engine.action_new_exposure(action)


def planned_active_spend(actions: list[PlanAction]) -> Decimal:
    return risk_engine.planned_active_spend(actions)


def quantity_budget(config: dict[str, Any]) -> Decimal:
    strategy = config.get("bid_strategy", {})
    quantity_strategy = config.get("quantity_strategy", {})
    budget = quantity_strategy.get("budget", strategy.get("max_cycle_spend", strategy.get("max_daily_spend", "0")))
    return Decimal(str(budget))


def allocate_quantities(prices: list[Decimal], config: dict[str, Any]) -> list[int]:
    try:
        return strategy_engine.allocate_quantities(prices, config)
    except strategy_engine.StrategyError as exc:
        raise YouPinError(str(exc)) from exc


def discover_candidates(client: YouPinClient, config: dict[str, Any]) -> list[StickerTemplate]:
    price_source = config.get("price_source", {})
    page_size = int(price_source.get("page_size", 50))
    max_pages = int(price_source.get("max_search_pages", 8))
    refresh_detail_price = bool(price_source.get("refresh_detail_price", False))

    by_id: dict[int, StickerTemplate] = {}
    keywords = config.get("keywords") or ["Sticker |"]
    for keyword in keywords:
        for page_index in range(1, max_pages + 1):
            payload = client.search_templates(str(keyword), page_index, page_size)
            records = deep_records(payload.get("Data", payload.get("data", payload)))
            templates = [template for template in (normalize_template(record) for record in records) if template]
            for template in templates:
                if is_target_team_sticker(template, config):
                    by_id[template.template_id] = template
            has_next = payload.get("HasNext", payload.get("hasNext"))
            total = payload.get("TotalCount", payload.get("totalCount"))
            if has_next is False:
                break
            if total is not None and page_index * page_size >= int(total):
                break
            if len(templates) < page_size:
                break
            time.sleep(0.25)

    candidates = list(by_id.values())
    if refresh_detail_price:
        refreshed: list[StickerTemplate] = []
        for template in candidates:
            try:
                detail = client.get_sell_detail(template.template_id)
                price = extract_lowest_sell_detail(detail) or template.lowest_sell_price
                refreshed.append(
                    StickerTemplate(
                        template_id=template.template_id,
                        name=template.name,
                        hash_name=template.hash_name,
                        lowest_sell_price=price,
                        on_sale_count=template.on_sale_count,
                        raw=template.raw,
                    )
                )
            except YouPinError as exc:
                log(f"刷新卖盘价失败，沿用搜索价：{template.name} ({exc})")
                refreshed.append(template)
            time.sleep(0.35)
        candidates = refreshed

    try:
        return strategy_engine.apply_selection_strategy(
            [item for item in candidates if item.lowest_sell_price is not None and item.lowest_sell_price > 0],
            config,
        )
    except strategy_engine.StrategyError as exc:
        raise YouPinError(str(exc)) from exc


def fetch_existing_orders(client: YouPinClient) -> dict[int, ExistingPurchaseOrder]:
    result: dict[int, ExistingPurchaseOrder] = {}
    page = 1
    while True:
        payload = client.search_my_purchase_orders(status=20, page_index=page, page_size=40)
        ensure_success(payload, "读取我的求购单")
        records = deep_records(payload.get("data", payload.get("Data", payload)))
        orders = [order for order in (normalize_existing_order(record) for record in records) if order]
        for order in orders:
            result[order.template_id] = order
        if len(orders) < 40:
            break
        page += 1
        time.sleep(0.4)
    return result


def fetch_inventory_items(
    client: YouPinClient,
    config: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> list[InventoryItem]:
    inventory_config = (config or {}).get("inventory", {})
    page_size = max(1, int(inventory_config.get("page_size", 50)))
    max_pages = max(1, int(inventory_config.get("max_pages", 20)))
    use_v3 = bool(inventory_config.get("use_v3", True))
    v3_page_size = max(1, int(inventory_config.get("v3_page_size", 5)))
    refresh = bool(inventory_config.get("refresh", False))
    result: list[InventoryItem] = []
    seen: set[str] = set()
    total_count: int | None = None

    if use_v3:
        try:
            for page in range(1, max_pages + 1):
                payload = client.get_inventory_v3(page_index=page, page_size=v3_page_size, refresh=refresh and page == 1)
                ensure_success(payload, "读取库存仓库")
                data = payload.get("Data", payload.get("data", {}))
                if not isinstance(data, dict):
                    records = deep_records(data)
                else:
                    if total_count is None:
                        parsed_total = parse_count_text(data.get("TotalCount", data.get("totalCount", data.get("Total", data.get("total")))))
                        total_count = parsed_total or None
                    records_value = data.get("ItemsInfos", data.get("itemsInfos", data.get("items", [])))
                    records = records_value if isinstance(records_value, list) else deep_records(records_value)

                items = [item for item in (normalize_inventory_item(record) for record in records if isinstance(record, dict)) if item]
                before = len(result)
                for item in items:
                    dedupe_key = item.asset_id or f"{page}:{len(result)}:{item.name}"
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    result.append(item)
                if not items or len(result) == before:
                    break
                if len(items) < v3_page_size:
                    break
                if total_count is not None and len(result) >= total_count:
                    break
                time.sleep(0.25)
            if result:
                return apply_inferred_buy_prices_to_inventory(result, config or {}, state)
        except YouPinError as exc:
            log(f"V3 库存接口读取失败，回退 PC 仓库接口：{exc}")
            result = []
            seen = set()
            total_count = None

    for page in range(1, max_pages + 1):
        payload = client.get_inventory_list(page_index=page, page_size=page_size)
        ensure_success(payload, "读取库存仓库")
        data = payload.get("data", payload.get("Data", {}))
        if not isinstance(data, dict):
            records = deep_records(data)
        else:
            if total_count is None:
                parsed_total = parse_count_text(data.get("totalCount", data.get("total", data.get("count"))))
                total_count = parsed_total or None
            records_value = data.get("itemsInfos", data.get("items", data.get("list", [])))
            records = records_value if isinstance(records_value, list) else deep_records(records_value)

        items = [item for item in (normalize_inventory_item(record) for record in records if isinstance(record, dict)) if item]
        for item in items:
            dedupe_key = item.asset_id or f"{page}:{len(result)}:{item.name}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            result.append(item)

        if len(items) < page_size:
            break
        if total_count is not None and len(result) >= total_count:
            break
        time.sleep(0.25)

    return apply_inferred_buy_prices_to_inventory(result, config or {}, state)


def exact_template_match(item: InventoryItem, template: StickerTemplate) -> bool:
    item_hash = normalize_text(item.market_hash_name)
    template_hash = normalize_text(template.hash_name)
    item_name = normalize_text(item.name)
    template_name = normalize_text(template.name)
    return bool(
        (item_hash and item_hash == template_hash)
        or (item_hash and item_hash == template_name)
        or (item_name and item_name == template_name)
        or (item_name and item_name == template_hash)
    )


def resolve_inventory_template(client: YouPinClient, item: InventoryItem) -> StickerTemplate | None:
    if item.template_id is not None:
        return StickerTemplate(
            template_id=item.template_id,
            name=item.name,
            hash_name=item.market_hash_name,
            lowest_sell_price=item.reference_price,
            on_sale_count=None,
            raw=item.raw,
        )

    keywords = []
    if item.market_hash_name:
        keywords.append(item.market_hash_name)
    if item.name and item.name not in keywords:
        keywords.append(item.name)

    for keyword in keywords:
        payload = client.search_templates(keyword, 1, 20)
        ensure_success(payload, f"解析库存模板 {keyword}")
        records = deep_records(payload.get("Data", payload.get("data", payload)))
        templates = [template for template in (normalize_template(record) for record in records) if template]
        for template in templates:
            if exact_template_match(item, template):
                return template
        if templates:
            return templates[0]
        time.sleep(0.25)
    return None


def extract_on_sale_count(payload: dict[str, Any]) -> int | None:
    data = payload.get("Data", payload.get("data"))
    if isinstance(data, dict):
        for key in ["totalCount", "TotalCount", "total", "Total", "count", "Count"]:
            parsed = parse_count_text(data.get(key))
            if parsed:
                return parsed
        for key in ["CommodityList", "commodityList", "list", "List"]:
            value = data.get(key)
            if isinstance(value, list):
                return len(value)
    elif isinstance(data, list):
        return len(data)
    return None


def fetch_template_sale_book(
    client: YouPinClient,
    template_id: int,
    *,
    page_size: int = 100,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    page_size = max(1, min(int(page_size), 100))
    max_pages = max(1, int(max_pages))

    for page in range(1, max_pages + 1):
        payload = client.get_sell_detail(template_id, page_index=page, page_size=page_size)
        ensure_success(payload, f"读取模板 {template_id} 在售盘口")
        data = payload.get("Data", payload.get("data", {}))
        if not isinstance(data, dict):
            break
        records = data.get("CommodityList", data.get("commodityList", []))
        if not isinstance(records, list):
            break

        for record in records:
            if not isinstance(record, dict):
                continue
            identity = str(
                pick(record, ["Id", "id", "CommodityId", "commodityId", "CommodityNo", "commodityNo"])
                or ""
            )
            dedupe_key = identity or f"{page}:{len(result)}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            result.append(record)

        has_next = bool(data.get("hasNext", data.get("HasNext", False)))
        if not has_next or not records:
            break
        time.sleep(0.15)
    return result


def build_market_snapshot(client: YouPinClient, item: InventoryItem) -> MarketSnapshot:
    template = resolve_inventory_template(client, item)
    if template is None:
        return MarketSnapshot(
            template_id=None,
            name=item.name,
            market_hash_name=item.market_hash_name,
            lowest_sell_price=item.reference_price,
            highest_purchase_price=None,
            on_sale_count=None,
            raw={},
        )

    lowest_sell = template.lowest_sell_price
    on_sale_count = template.on_sale_count
    try:
        sale_payload = client.get_on_sale_commodity_list(template.template_id, page_index=1, page_size=10)
        lowest_sell = extract_lowest_sell_detail(sale_payload) or lowest_sell
        on_sale_count = extract_on_sale_count(sale_payload) or on_sale_count
    except YouPinError:
        try:
            sale_payload = client.get_sell_detail(template.template_id)
            lowest_sell = extract_lowest_sell_detail(sale_payload) or lowest_sell
            on_sale_count = extract_on_sale_count(sale_payload) or on_sale_count
        except YouPinError:
            pass

    highest_purchase = None
    try:
        purchase_payload = client.get_template_purchase_orders(template.template_id)
        highest_purchase = extract_highest_purchase_price(purchase_payload)
    except YouPinError:
        pass

    return MarketSnapshot(
        template_id=template.template_id,
        name=template.name,
        market_hash_name=template.hash_name,
        lowest_sell_price=lowest_sell,
        highest_purchase_price=highest_purchase,
        on_sale_count=on_sale_count,
        raw=template.raw,
    )


def exit_rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    return exit_engine.rules(config)


def inventory_matches_exit_rule(item: InventoryItem, rule: dict[str, Any]) -> bool:
    return exit_engine.matches_rule(item, rule)


def exit_rule_target(rule: dict[str, Any]) -> Decimal | None:
    return exit_engine.rule_target(rule)


def exit_rule_action_mode(rule: dict[str, Any]) -> str:
    return exit_engine.rule_action_mode(rule)


def exit_action_price(value: Decimal, config: dict[str, Any]) -> Decimal:
    return exit_engine.action_price(value, config)


def make_exit_action(item: InventoryItem, rule: dict[str, Any], snapshot: MarketSnapshot, config: dict[str, Any]) -> ExitAction:
    target = exit_rule_target(rule)
    if target is None:
        return ExitAction("skip", item, Decimal("0"), None, snapshot, rule, "规则缺少预期价格")

    mode = exit_rule_action_mode(rule)
    allow_bid = mode in {"bid_or_sale", "bid_only"}
    allow_sale = mode in {"bid_or_sale", "sale_only"}
    highest_bid = snapshot.highest_purchase_price
    lowest_sell = snapshot.lowest_sell_price

    if item.status_text != "可交易":
        return ExitAction("wait_cd", item, target, None, snapshot, rule, f"库存仍为 {item.display_cooldown}")

    if allow_bid and highest_bid is not None and highest_bid >= target:
        price = exit_action_price(highest_bid, config)
        return ExitAction("drop_to_bid", item, target, price, snapshot, rule, f"最高求购 {highest_bid} 已达到预期 {target}")

    if allow_sale and lowest_sell is not None and lowest_sell >= target:
        price = exit_engine.sale_action_price(target, lowest_sell, config)
        return ExitAction("list_sale", item, target, price, snapshot, rule, f"最低在售 {lowest_sell} 已达到预期 {target}")

    return ExitAction("hold", item, target, None, snapshot, rule, f"未达标：最高求购 {highest_bid or '-'}，最低在售 {lowest_sell or '-'}")


def _exit_actionable_count(actions: list[ExitAction]) -> int:
    return len([candidate for candidate in actions if candidate.is_actionable])


def _prepare_rule_exit_actions(
    client: YouPinClient,
    config: dict[str, Any],
    inventory: list[InventoryItem],
    snapshots: dict[str, MarketSnapshot],
    max_actions: int,
) -> list[ExitAction]:
    active_rules = exit_rules(config)
    if not active_rules:
        return []

    actions: list[ExitAction] = []
    per_rule_counts: dict[int, int] = {}

    for item in inventory:
        for rule_index, rule in enumerate(active_rules):
            if not inventory_matches_exit_rule(item, rule):
                continue
            max_quantity = max(1, int(rule.get("max_quantity", 1)))
            if per_rule_counts.get(rule_index, 0) >= max_quantity:
                continue
            key = item.market_hash_name or item.name
            snapshot = snapshots.get(key)
            if snapshot is None:
                snapshot = build_market_snapshot(client, item)
                snapshots[key] = snapshot
                time.sleep(0.25)
            action = make_exit_action(item, rule, snapshot, config)
            actions.append(action)
            per_rule_counts[rule_index] = per_rule_counts.get(rule_index, 0) + 1
            if _exit_actionable_count(actions) >= max_actions:
                return actions
            break
    return actions


def _prepare_plugin_exit_actions(
    client: YouPinClient,
    config: dict[str, Any],
    inventory: list[InventoryItem],
    snapshots: dict[str, MarketSnapshot],
    existing_actions: list[ExitAction],
    max_actions: int,
) -> list[ExitAction]:
    remaining = max_actions - _exit_actionable_count(existing_actions)
    if remaining <= 0:
        return []

    plugin_actions: list[ExitAction] = []
    seen_asset_ids = {str(action.item.asset_id) for action in existing_actions if action.item.asset_id}
    context = {
        "build_market_snapshot": build_market_snapshot,
        "fetch_template_sale_book": fetch_template_sale_book,
        "make_exit_action": make_exit_action,
        "snapshots": snapshots,
        "max_actions": max_actions,
        "remaining_actions": remaining,
        "log": log,
    }

    for spec in exit_engine.plugin_specs(config):
        try:
            builder = exit_engine.load_plugin_builder(spec, config)
            context["plugin_config"] = spec
            result = builder(client, inventory, config, context)
        except strategy_engine.StrategyError as exc:
            raise YouPinError(str(exc)) from exc
        if result is None:
            continue
        if not isinstance(result, list):
            raise YouPinError(f"exit plugin {spec.get('mode')} must return list[ExitAction]")
        for action in result:
            if not isinstance(action, ExitAction):
                raise YouPinError(f"exit plugin {spec.get('mode')} returned invalid action")
            asset_id = str(action.item.asset_id)
            if asset_id and asset_id in seen_asset_ids:
                continue
            plugin_actions.append(action)
            if asset_id:
                seen_asset_ids.add(asset_id)
            if _exit_actionable_count(existing_actions + plugin_actions) >= max_actions:
                return plugin_actions
    return plugin_actions


def prepare_exit_actions(client: YouPinClient, config: dict[str, Any]) -> list[ExitAction]:
    strategy = config.get("exit_strategy", {})
    active_rules = exit_rules(config)
    active_plugins = exit_engine.plugin_specs(config)
    if not active_rules and not active_plugins:
        return []

    inventory = fetch_inventory_items(client, config)
    max_actions = max(1, int(strategy.get("max_actions_per_cycle", 5)))
    snapshots: dict[str, MarketSnapshot] = {}
    actions = _prepare_rule_exit_actions(client, config, inventory, snapshots, max_actions)
    actions.extend(_prepare_plugin_exit_actions(client, config, inventory, snapshots, actions, max_actions))
    return actions


def exit_action_profit_ok(action: ExitAction) -> tuple[bool, str]:
    min_ratio = to_decimal(action.rule.get("min_profit_ratio"))
    if min_ratio is None:
        return True, ""
    buy_price = action.item.buy_price
    action_price = action.action_price
    if buy_price is None or buy_price <= 0:
        return False, f"missing buy_price for profit guard ratio {min_ratio}"
    if action_price is None or action_price <= 0:
        return False, f"missing action_price for profit guard ratio {min_ratio}"
    required = buy_price * (Decimal("1") + min_ratio)
    if action_price < required:
        return False, f"price {action_price} below required {required} for buy {buy_price} ratio {min_ratio}"
    return True, ""


def _wait_for_relist_assets(
    client: YouPinClient,
    asset_ids: set[str],
    config: dict[str, Any],
) -> set[str]:
    if not asset_ids:
        return set()
    strategy = config.get("exit_strategy", {})
    timeout_seconds = max(0.0, float(strategy.get("relist_wait_seconds", 15)))
    poll_seconds = max(0.2, float(strategy.get("relist_poll_seconds", 1)))
    deadline = time.monotonic() + timeout_seconds

    while True:
        payload = client.get_inventory_v3(page_index=1, page_size=1000, refresh=False)
        ensure_success(payload, "确认下架库存状态")
        data = payload.get("Data", payload.get("data", {}))
        records = data.get("ItemsInfos", data.get("itemsInfos", [])) if isinstance(data, dict) else []
        statuses: dict[str, int | None] = {}
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            asset_id = str(pick(record, ["SteamAssetId", "steamAssetId", "AssetId", "assetId"]) or "")
            if asset_id not in asset_ids:
                continue
            raw_status = pick(record, ["AssetStatus", "assetStatus"])
            try:
                statuses[asset_id] = int(raw_status)
            except (TypeError, ValueError):
                statuses[asset_id] = None

        ready = {asset_id for asset_id in asset_ids if statuses.get(asset_id) == 0}
        if ready == asset_ids or time.monotonic() >= deadline:
            return ready
        time.sleep(poll_seconds)


def _retry_sell_after_known_reminder(
    client: YouPinClient,
    response: dict[str, Any],
    item_infos: list[dict[str, Any]],
    strategy: dict[str, Any],
) -> dict[str, Any]:
    if response_code(response) != 7000002:
        return response

    error_data = response.get("errorData", response.get("ErrorData", {}))
    if not isinstance(error_data, dict):
        return response
    scene_name = str(error_data.get("sceneName", error_data.get("SceneName", "")) or "")
    scene_code = str(error_data.get("sceneCode", error_data.get("SceneCode", "")) or "")
    allowed_name = "出售场景交易撤回提示"
    enabled = bool(strategy.get("auto_acknowledge_trade_withdrawal_notice", False))
    if not enabled or scene_name != allowed_name or not scene_code:
        raise YouPinError(
            f"悠悠要求确认安全提示：{scene_name or '未知提示'}。"
            "程序没有自动接受未知提示，请先在 APP 确认，或仅为已审核提示开启自动确认。"
        )

    countdown = error_data.get("countdown", error_data.get("Countdown", 0))
    try:
        wait_seconds = max(0.0, min(float(countdown), 10.0))
    except (TypeError, ValueError):
        wait_seconds = 0.0
    log(f"[EXIT NOTICE] 确认悠悠安全提示：{scene_name}")
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    acknowledge = client.acknowledge_global_reminder(scene_code)
    ensure_success(acknowledge, f"确认悠悠安全提示 {scene_name}")
    return client.sell_inventory_items(item_infos)


def execute_exit_actions(client: YouPinClient, actions: list[ExitAction], config: dict[str, Any], live: bool) -> list[ExitAction]:
    strategy = config.get("exit_strategy", {})
    enabled = bool(strategy.get("enabled", True))
    if not enabled:
        log("出货策略未启用，跳过")
        return []
    actionable = [action for action in actions if action.is_actionable]
    if not actionable:
        log("没有达到出货条件的库存")
        return []
    max_actions = max(1, int(strategy.get("max_actions_per_cycle", 5)))
    actionable = actionable[:max_actions]

    guarded: list[ExitAction] = []
    for action in actionable:
        if action.action == "delist_sale":
            guarded.append(action)
            continue
        ok, reason = exit_action_profit_ok(action)
        if not ok:
            log(f"[EXIT SKIP profit_guard] {action.item.name} | price={action.action_price} | {reason}")
            continue
        guarded.append(action)
    actionable = guarded
    if not actionable:
        log("没有通过利润风控的出货动作")
        return []

    for action in actionable:
        log(
            f"[EXIT {action.action}] {action.item.name} | target={action.target_price} | "
            f"price={action.action_price} | {action.reason}"
        )

    if config.get("dry_run", True) or not live:
        log("dry_run 或未启用 live：只预览出货对账动作，不真实下架/上架/丢求购")
        return []

    submitted: list[ExitAction] = []
    relist_after_delist: list[ExitAction] = []
    off_shelf_interval = max(0.0, float(strategy.get("off_shelf_interval_seconds", 0.2)))

    for action in actionable:
        if action.action not in {"delist_sale", "relist_sale"}:
            continue
        commodity_id = action.item.commodity_id
        if not commodity_id:
            log(f"[EXIT SKIP missing_commodity_id] {action.item.name} | asset={action.item.asset_id}")
            continue
        try:
            response = client.off_shelf_commodity(commodity_id)
            ensure_delete_success(response, f"下架商品 {commodity_id}")
        except YouPinError as exc:
            log(f"[EXIT ERROR delist_sale] {action.item.name} | commodity={commodity_id} | {exc}")
            continue
        if action.action == "relist_sale":
            relist_after_delist.append(action)
        else:
            submitted.append(action)
        if off_shelf_interval > 0:
            time.sleep(off_shelf_interval)

    ready_relist_ids = _wait_for_relist_assets(
        client,
        {action.item.asset_id for action in relist_after_delist if action.item.asset_id},
        config,
    )
    for action in relist_after_delist:
        if action.item.asset_id not in ready_relist_ids:
            log(
                f"[EXIT WAIT relist_sale] {action.item.name} | asset={action.item.asset_id} | "
                "已下架但库存状态尚未恢复，本轮不重复上架，下轮自动补齐"
            )

    sale_actions = [
        action
        for action in actionable
        if action.action in {"list_sale", "drop_to_bid"}
        or (action.action == "relist_sale" and action.item.asset_id in ready_relist_ids)
    ]
    item_infos = []
    unique_sale_actions: list[ExitAction] = []
    seen_assets: set[str] = set()
    for action in sale_actions:
        if action.action_price is None:
            continue
        asset_id = str(action.item.asset_id)
        if not asset_id or asset_id in seen_assets:
            continue
        seen_assets.add(asset_id)
        item_infos.append(
            {
                "AssetId": int(asset_id),
                "Price": str(action.action_price),
                "Remark": "",
                "PriceCheckFlag": True,
            }
        )
        unique_sale_actions.append(action)
    if not item_infos:
        return submitted
    response = client.sell_inventory_items(item_infos)
    response = _retry_sell_after_known_reminder(client, response, item_infos, strategy)
    ensure_sell_success(response, f"出货 {len(item_infos)} 件库存")
    submitted.extend(unique_sale_actions)
    return submitted


def calculate_bid_price(
    client: YouPinClient,
    template: StickerTemplate,
    config: dict[str, Any],
    existing_order: ExistingPurchaseOrder | None = None,
) -> tuple[Decimal | None, str]:
    try:
        return strategy_engine.calculate_bid_price(client, template, config, existing_order)
    except strategy_engine.StrategyError as exc:
        raise YouPinError(str(exc)) from exc


def build_plan(
    client: YouPinClient,
    candidates: list[StickerTemplate],
    existing_orders: dict[int, ExistingPurchaseOrder],
    config: dict[str, Any],
) -> list[PlanAction]:
    top_n = int(config.get("top_n", 10))
    selected = candidates[:top_n]
    quantity_config = config.get("quantity_strategy", {})
    min_purchase_num = max(0, int(quantity_config.get("min_quantity_per_item", 1)))
    max_purchase_num = max(min_purchase_num, int(quantity_config.get("max_quantity_per_item", 999999)))
    actions: list[PlanAction] = []
    priced: list[tuple[StickerTemplate, Decimal, str]] = []
    for template in selected:
        existing = existing_orders.get(template.template_id)
        try:
            price, reason = strategy_engine.calculate_bid_price(client, template, config, existing)
        except strategy_engine.StrategyError as exc:
            raise YouPinError(str(exc)) from exc
        if price is None:
            log(f"跳过：{template.name}，{reason}")
            continue
        priced.append((template, price, reason))

    try:
        quantities = strategy_engine.allocate_quantities([item[1] for item in priced], config)
    except strategy_engine.StrategyError as exc:
        raise YouPinError(str(exc)) from exc
    for (template, price, reason), purchase_num in zip(priced, quantities):
        if purchase_num <= 0:
            log(f"跳过：{template.name}，预算不足以分配数量")
            continue
        if min_purchase_num > 1 and purchase_num < min_purchase_num:
            log(
                f"skip: {template.name}; planned quantity {purchase_num} "
                f"is below min purchase quantity {min_purchase_num}"
            )
            continue
        existing = existing_orders.get(template.template_id)
        if (
            existing
            and existing.purchase_num is not None
            and purchase_num < existing.purchase_num <= max_purchase_num
        ):
            purchase_num = existing.purchase_num
        action = "create"
        if existing:
            if existing.purchase_price == price and existing.purchase_num == purchase_num:
                action = "keep"
            else:
                action = "update"
        actions.append(
            PlanAction(
                action=action,
                template=template,
                bid_price=price,
                purchase_num=purchase_num,
                reference_price=template.lowest_sell_price,
                existing_order=existing,
                reason=reason,
            )
        )
    return actions


def cleanup_strategy_config(config: dict[str, Any]) -> dict[str, Any]:
    return order_reconciler.cleanup_strategy_config(config)


def order_matches_cleanup_scope(order: ExistingPurchaseOrder, cleanup: dict[str, Any]) -> bool:
    return order_reconciler.order_matches_cleanup_scope(order, cleanup)


def build_cleanup_plan(
    actions: list[PlanAction],
    existing_orders: dict[int, ExistingPurchaseOrder],
    config: dict[str, Any],
) -> list[CleanupAction]:
    return order_reconciler.build_cleanup_plan(actions, existing_orders, config)


def enforce_budget(actions: list[PlanAction], config: dict[str, Any], state: dict[str, Any], live: bool) -> None:
    risk_engine.enforce_budget(actions, config, state, live)


def save_state(
    config: dict[str, Any],
    actions: list[PlanAction],
    submitted_actions: list[PlanAction],
    cleanup_actions: list[CleanupAction] | None = None,
    submitted_cleanup_actions: list[CleanupAction] | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    state_file = state_file_path(config)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    submitted_exposure = actionable_spend(submitted_actions)
    active_spend = planned_active_spend(actions)
    data = {
        "updated_at": now_text(),
        "daily_budget": {
            "date": today_key(),
            "planned_active_spend": str(active_spend),
            "last_submitted_exposure": str(submitted_exposure),
        },
        "purchase_risk": (state or {}).get("purchase_risk"),
        "last_submit_at_by_item": (state or {}).get("last_submit_at_by_item", {}),
        "last_seen_purchase_orders": (state or {}).get("last_seen_purchase_orders", []),
        "purchase_price_fill_events": (state or {}).get("purchase_price_fill_events", []),
        "purchase_price_fill_updated_at": (state or {}).get("purchase_price_fill_updated_at"),
        "last_inventory_asset_ids": (state or {}).get("last_inventory_asset_ids", []),
        "last_inventory_seen_at": (state or {}).get("last_inventory_seen_at"),
        "cs_update_guard": (state or {}).get("cs_update_guard"),
        "cleanup_orders": [
            {
                "template_id": action.order.template_id,
                "name": action.order.commodity_name,
                "purchase_price": str(action.order.purchase_price) if action.order.purchase_price is not None else None,
                "purchase_num": action.order.purchase_num,
                "supply_quantity": action.order.supply_quantity,
                "fill": action.order.fill_text,
                "spend": str(action.spend),
                "rank": action.order.rank,
                "order_no": action.order.order_no,
                "reason": action.reason,
            }
            for action in cleanup_actions or []
        ],
        "last_deleted_cleanup_orders": [
            {
                "template_id": action.order.template_id,
                "name": action.order.commodity_name,
                "purchase_price": str(action.order.purchase_price) if action.order.purchase_price is not None else None,
                "purchase_num": action.order.purchase_num,
                "supply_quantity": action.order.supply_quantity,
                "fill": action.order.fill_text,
                "spend": str(action.spend),
                "rank": action.order.rank,
                "order_no": action.order.order_no,
                "reason": action.reason,
            }
            for action in submitted_cleanup_actions or []
        ],
        "actions": [
            {
                "action": action.action,
                "template_id": action.template.template_id,
                "name": action.template.name,
                "reference_price": str(action.reference_price) if action.reference_price is not None else None,
                "bid_price": str(action.bid_price),
                "purchase_num": action.purchase_num,
                "spend": str(action.spend),
                "selection_score": action.template.raw.get("_selection_score"),
                "selection_reason": action.template.raw.get("_selection_reason"),
                "existing_rank": action.existing_order.rank if action.existing_order else None,
                "existing_supply_quantity": action.existing_order.supply_quantity if action.existing_order else None,
                "existing_fill": action.existing_order.fill_text if action.existing_order else None,
                "existing_purchase_price": str(action.existing_order.purchase_price) if action.existing_order and action.existing_order.purchase_price is not None else None,
                "book_top_purchase_price": str(action.existing_order.max_purchase_price) if action.existing_order and action.existing_order.max_purchase_price is not None else None,
                "existing_order_no": action.existing_order.order_no if action.existing_order else None,
            }
            for action in actions
        ],
    }
    state_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def refresh_interval_seconds(config: dict[str, Any]) -> int:
    poll_seconds = int(config.get("poll_seconds") or 0)
    if poll_seconds > 0:
        return max(60, poll_seconds)

    refreshes_per_day = int(config.get("refreshes_per_day", 4))
    if refreshes_per_day <= 0:
        raise YouPinError("refreshes_per_day 必须大于 0，或直接设置 poll_seconds。")
    return max(60, 86400 // refreshes_per_day)


def purchase_automation_enabled(config: dict[str, Any]) -> bool:
    automation = config.get("purchase_automation", {})
    if isinstance(automation, bool):
        return automation
    if not isinstance(automation, dict):
        return True
    return bool(automation.get("enabled", True))


def execute_plan(
    client: YouPinClient,
    actions: list[PlanAction],
    config: dict[str, Any],
    live: bool,
    state: dict[str, Any] | None = None,
) -> list[PlanAction]:
    dry_run = bool(config.get("dry_run", True))
    effective_live = live and not dry_run
    strategy = config.get("bid_strategy", {})
    execution_config = config.get("execution", {})
    create_channel = str(execution_config.get("create_order_channel", "api"))
    command_file = str(execution_config.get("emulator_command_file", execution_bridge.DEFAULT_COMMAND_FILE))
    if not purchase_automation_enabled(config):
        cancelled = execution_bridge.cancel_stale_create_commands(
            [],
            command_file,
            reason="purchase_automation_paused",
        )
        log(f"求购自动化已暂停，跳过全部求购动作；取消待执行模拟器指令 {len(cancelled)} 个")
        return []
    modify_interval_seconds = max(0.0, float(strategy.get("modify_interval_seconds", 0)))
    allow_create_orders = bool(strategy.get("allow_create_orders", True))
    submitted_actions: list[PlanAction] = []
    last_submit_at_by_item: dict[str, float] = {}
    state_ref = state if state is not None else load_state(config)
    state_submit_times = state_ref.setdefault("last_submit_at_by_item", {})
    if not isinstance(state_submit_times, dict):
        state_submit_times = {}
        state_ref["last_submit_at_by_item"] = state_submit_times
    if effective_live and create_channel in {"emulator_queue", "app_queue"}:
        cancelled = execution_bridge.cancel_stale_create_commands(actions, command_file)
        if cancelled:
            log(f"cancelled {len(cancelled)} stale emulator create command(s)")
    for idx, action in enumerate(actions, start=1):
        prefix = (
            f"{idx:02d}. {action.action.upper()} "
            f"{action.template.name} | template={action.template.template_id} | "
            f"sell={action.reference_price} | bid={action.bid_price} x {action.purchase_num}"
        )
        if action.existing_order and action.existing_order.rank is not None:
            prefix += f" | current_rank={action.existing_order.rank}"
        if action.existing_order and action.existing_order.max_purchase_price is not None:
            prefix += f" | book_top={action.existing_order.max_purchase_price}"
        if action.existing_order and action.existing_order.purchase_price is not None:
            prefix += f" | current_bid={action.existing_order.purchase_price}"
        if not effective_live or action.action == "keep":
            log(prefix)
            continue
        if action.action == "create" and not allow_create_orders:
            log(prefix + " | SKIP create_disabled")
            continue
        if action.action == "create" and create_channel in {"emulator_queue", "app_queue"}:
            queued = execution_bridge.queue_create_commands([action], command_file, source="core_execute")
            command_id = queued[0].get("id", "") if queued else ""
            log(prefix + f" | QUEUED emulator_create id={command_id}")
            continue

        modify_cd_key = str(action.template.template_id)
        last_submit_at = last_submit_at_by_item.get(modify_cd_key)
        if last_submit_at is not None and modify_interval_seconds > 0:
            wait_seconds = modify_interval_seconds - (time.monotonic() - last_submit_at)
            if wait_seconds > 0:
                log(f"等待 {wait_seconds:.0f} 秒后继续，遵守 {action.template.name} 单品求购修改间隔")
                time.sleep(wait_seconds)

        state_submit_at = parse_state_datetime(state_submit_times.get(modify_cd_key))
        if state_submit_at is not None and modify_interval_seconds > 0:
            state_wait_seconds = modify_interval_seconds - (datetime.now() - state_submit_at).total_seconds()
            if state_wait_seconds > 0:
                log(f"等待 {state_wait_seconds:.0f} 秒后继续，遵守 {action.template.name} 单品求购修改间隔")
                time.sleep(state_wait_seconds)

        order_no = action.existing_order.order_no if action.existing_order else ""
        supply_quantity = action.existing_order.supply_quantity if action.existing_order else 0
        response = client.publish_purchase_order(
            action.template,
            action.bid_price,
            action.purchase_num,
            order_no=order_no,
            supply_quantity=supply_quantity,
        )
        code = response_code(response)
        msg = response_msg(response)
        if code in {30010020, "30010020"} or "修改过于频繁" in msg:
            state_submit_times[modify_cd_key] = datetime.now().isoformat(timespec="seconds")
            log(prefix + f" | SKIP cooldown: {msg or code}")
            continue
        ensure_success(response, f"{action.action} {action.template.name}")
        submitted_actions.append(action)
        last_submit_at_by_item[modify_cd_key] = time.monotonic()
        state_submit_times[modify_cd_key] = datetime.now().isoformat(timespec="seconds")
        log(prefix + " -> OK")
    return submitted_actions


def execute_cleanup_plan(
    client: YouPinClient,
    cleanup_actions: list[CleanupAction],
    config: dict[str, Any],
    live: bool,
) -> list[CleanupAction]:
    if not cleanup_actions:
        return []
    if not purchase_automation_enabled(config):
        log("求购自动化已暂停，跳过求购单清理与重排")
        return []
    dry_run = bool(config.get("dry_run", True))
    cleanup = cleanup_strategy_config(config)
    effective_live = live and not dry_run and cleanup["auto_execute"]
    prefix = "待撤销/重置求购" if not effective_live else "撤销/重置求购"
    for idx, action in enumerate(cleanup_actions, start=1):
        order = action.order
        log(
            f"{prefix} {idx:02d}. {order.commodity_name} | "
            f"template={order.template_id} | bid={order.purchase_price} x {order.purchase_num} | "
            f"filled={order.fill_text} | rank={order.rank} | reason={action.reason}"
        )
    if not effective_live:
        return []
    order_nos = [action.order.order_no for action in cleanup_actions if action.order.order_no]
    response = client.delete_purchase_orders(order_nos)
    ensure_delete_success(response, f"撤销/重置 {len(order_nos)} 个求购单")
    log(f"已撤销/重置 {len(order_nos)} 个求购单")
    return cleanup_actions


def prepare_cycle_plan(client: YouPinClient, config: dict[str, Any]) -> CyclePlan:
    state = load_state(config)
    log("开始扫描候选贴纸")
    candidates = discover_candidates(client, config)
    if not candidates:
        log("没有找到候选贴纸；可能是未登录、关键词不匹配，或者商品已经下架。")
        return CyclePlan(candidates=[], actions=[], cleanup_actions=[], existing_orders={}, state=state)

    selection_mode = str(config.get("selection_strategy", {}).get("mode", "potential"))
    log(
        f"命中候选 {len(candidates)} 个，"
        f"{selection_strategy_label(selection_mode)}({selection_mode}) 策略前 {min(len(candidates), int(config.get('top_n', 10)))} 个："
    )
    for idx, item in enumerate(candidates[: int(config.get("top_n", 10))], start=1):
        selection_reason = item.raw.get("_selection_reason")
        if selection_reason:
            log(
                f"  {idx:02d}. {item.name} | template={item.template_id} | "
                f"sell={item.lowest_sell_price} | on_sale={item.on_sale_count} | {selection_reason}"
            )
        else:
            log(f"  {idx:02d}. {item.name} | template={item.template_id} | sell={item.lowest_sell_price} | on_sale={item.on_sale_count}")

    existing_orders: dict[int, ExistingPurchaseOrder] = {}
    if config.get("manage_existing_orders", True):
        try:
            existing_orders = fetch_existing_orders(client)
            update_purchase_price_fill_events(state, existing_orders, config)
            log(f"读取到当前有效求购单 {len(existing_orders)} 个")
        except YouPinError as exc:
            log(f"读取已有求购单失败，本轮只按新建计划预览：{exc}")

    plan_config = copy_config_for_budget(config)
    risk_snapshot = build_purchase_risk_snapshot(client, config, existing_orders, state=state)
    if risk_snapshot.enabled:
        remaining = risk_snapshot.remaining_success_budget
        state["purchase_risk"] = risk_snapshot.to_dict()
        window_start = risk_snapshot.window_start or f"最近 {risk_snapshot.lookback_hours} 小时"
        log(
            f"24小时成交风控：起算 {window_start}，成交 "
            f"{risk_snapshot.recent_success_spend}（{risk_snapshot.recent_success_count} 件），"
            f"上限 {risk_snapshot.limit}，剩余额度 {remaining}"
        )
        if risk_snapshot.active_order_exposure_limit > 0:
            log(
                f"活跃挂单风控：当前挂单风险 {risk_snapshot.active_order_spend}，"
                f"挂单风险上限 {risk_snapshot.active_order_exposure_limit}"
            )
        if remaining <= 0:
            log("24小时成交额度已用尽，本轮不再生成求购动作。")
            cleanup_actions = build_cleanup_plan([], existing_orders, plan_config)
            return CyclePlan(candidates=candidates, actions=[], cleanup_actions=cleanup_actions, existing_orders=existing_orders, state=state)
        if risk_snapshot.include_active_order_exposure and risk_snapshot.limit > 0:
            quantity = plan_config.setdefault("quantity_strategy", {})
            strategy = plan_config.setdefault("bid_strategy", {})
            current_budget = quantity_budget(plan_config)
            capped_budget = min(current_budget, remaining) if current_budget > 0 else remaining
            quantity["budget"] = str(capped_budget)
            strategy["max_cycle_spend"] = str(capped_budget)
            strategy["max_daily_spend"] = str(capped_budget)
            log(f"24小时剩余额度纳入挂单预算：本轮求购预算限制为 {capped_budget}")
    actions = build_plan(client, candidates, existing_orders, plan_config)
    cleanup_actions = build_cleanup_plan(actions, existing_orders, plan_config)
    if cleanup_actions:
        log(f"待撤销/重置求购单 {len(cleanup_actions)} 个")
        for idx, action in enumerate(cleanup_actions, start=1):
            log(
                f"  CLEAN {idx:02d}. {action.order.commodity_name} | "
                f"template={action.order.template_id} | bid={action.order.purchase_price} x {action.order.purchase_num} | "
                f"filled={action.order.fill_text} | reason={action.reason}"
            )
    if risk_snapshot.enabled:
        risk_snapshot = build_purchase_risk_snapshot(client, config, existing_orders, actions, cleanup_actions, state=state)
        state["purchase_risk"] = risk_snapshot.to_dict()
        if risk_snapshot.limit > 0 and risk_snapshot.projected_total_risk > risk_snapshot.limit:
            log(
                f"24小时成交+活跃挂单风险 {risk_snapshot.projected_total_risk} 超过上限 "
                f"{risk_snapshot.limit}，本轮取消求购动作。"
            )
            actions = []
            cleanup_actions = build_cleanup_plan([], existing_orders, plan_config)
            risk_snapshot = build_purchase_risk_snapshot(client, config, existing_orders, actions, cleanup_actions, state=state)
            state["purchase_risk"] = risk_snapshot.to_dict()
    return CyclePlan(candidates=candidates, actions=actions, cleanup_actions=cleanup_actions, existing_orders=existing_orders, state=state)


def run_cycle(client: YouPinClient, config: dict[str, Any], live: bool) -> CyclePlan:
    guard_status = check_cs_update_guard(client, config, live=live)
    if guard_status.get("halted") and cs_update_guard_config(config)["pause_bidding_on_update"]:
        state = load_state(config)
        state["cs_update_guard"] = guard_status
        existing_orders: dict[int, ExistingPurchaseOrder] = {}
        if config.get("manage_existing_orders", True):
            try:
                existing_orders = fetch_existing_orders(client)
            except YouPinError as exc:
                log(f"CS update guard halted; failed to refresh existing orders: {exc}")
        log("CS update guard halted trading; skip bid strategy until guard state is reset.")
        return CyclePlan(candidates=[], actions=[], cleanup_actions=[], existing_orders=existing_orders, state=state)

    if not purchase_automation_enabled(config):
        state = load_state(config)
        state["purchase_automation"] = {
            "enabled": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        execution_config = config.get("execution", {})
        if not isinstance(execution_config, dict):
            execution_config = {}
        command_file = str(execution_config.get("emulator_command_file", execution_bridge.DEFAULT_COMMAND_FILE))
        cancelled = execution_bridge.cancel_stale_create_commands(
            [],
            command_file,
            reason="purchase_automation_paused",
        )
        log(f"求购自动化已暂停；本轮不扫描、不新建、不改价、不清理求购单，取消待执行指令 {len(cancelled)} 个")
        save_state(config, [], [], [], [], state)
        return CyclePlan(candidates=[], actions=[], cleanup_actions=[], existing_orders={}, state=state)

    plan = prepare_cycle_plan(client, config)
    if not plan.actions and not plan.cleanup_actions:
        save_state(config, [], [], [], [], plan.state)
        return plan
    cleanup = cleanup_strategy_config(config)
    pre_cleanup = bool(
        plan.cleanup_actions
        and live
        and not config.get("dry_run", True)
        and cleanup["auto_execute"]
    )
    submitted_cleanup_actions: list[CleanupAction] = []
    if pre_cleanup:
        submitted_cleanup_actions = execute_cleanup_plan(client, plan.cleanup_actions, config, live=live)
        if submitted_cleanup_actions:
            plan = prepare_cycle_plan(client, config)
            if not plan.actions and not plan.cleanup_actions:
                save_state(config, [], [], [], submitted_cleanup_actions, plan.state)
                return plan
    enforce_budget(plan.actions, config, plan.state, live=live and not config.get("dry_run", True))
    if not pre_cleanup:
        submitted_cleanup_actions = execute_cleanup_plan(client, plan.cleanup_actions, config, live=live)
    if submitted_cleanup_actions and not pre_cleanup:
        plan = prepare_cycle_plan(client, config)
        enforce_budget(plan.actions, config, plan.state, live=live and not config.get("dry_run", True))
    submitted_actions = execute_plan(client, plan.actions, config, live=live, state=plan.state)
    final_plan = prepare_cycle_plan(client, config) if submitted_actions or submitted_cleanup_actions else plan
    save_state(config, final_plan.actions, submitted_actions, final_plan.cleanup_actions, submitted_cleanup_actions, final_plan.state)
    return final_plan


def load_config(path: Path) -> dict[str, Any]:
    try:
        return config_engine.load_config(path)
    except FileNotFoundError as exc:
        raise YouPinError(f"找不到配置文件：{path}") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise YouPinError(f"配置文件不是合法 JSON：{path}: {exc}") from exc


def self_test() -> None:
    sample = {
        "Code": 0,
        "Data": [
            {"Id": 101, "CommodityName": "Sticker | Team Vitality (Holo) | Cologne 2026", "Price": "1.23"},
            {"Id": 102, "CommodityName": "Sticker | apEX (Holo) | Cologne 2026", "Price": "0.90"},
            {"Id": 103, "CommodityName": "Sticker | MOUZ (Holo) | Cologne 2026", "Price": "1.10"},
            {"Id": 104, "CommodityName": "印花 | HEROIC (Holo) | Cologne 2026", "Price": "19.09"},
            {"Id": 105, "CommodityName": "印花板 | Team Liquid | Cologne 2026", "Price": "15.90"},
        ],
    }
    config = {
        "event": "Cologne 2026",
        "finish": "Holo",
        "team_names": ["Team Vitality", "MOUZ", "HEROIC"],
    }
    records = deep_records(sample)
    templates = [template for template in (normalize_template(record) for record in records) if template]
    targets = [template for template in templates if is_target_team_sticker(template, config)]
    assert [item.template_id for item in targets] == [101, 103, 104], targets
    assert money(Decimal("1.234"), 2) == Decimal("1.23")
    inventory = normalize_inventory_item(
        {
            "steamAssetId": "52122398977",
            "templateId": 122300,
            "name": "印花 | 9z Team (Holo) | Cologne 2026",
            "marketHashName": "Sticker | 9z Team (Holo) | Cologne 2026",
            "cacheExpirationDesc": "5天16小时",
            "Tradable": False,
            "assetBuyPrice": "24.10",
            "price": 29.9,
        }
    )
    assert inventory is not None
    assert inventory.template_id == 122300
    assert inventory.buy_price == Decimal("24.10")
    assert inventory.reference_price == Decimal("29.9")
    assert inventory.display_cooldown == "5天16小时"
    assert inventory.status_text == "CD中"
    recent_inventory = normalize_inventory_item(
        {
            "steamAssetId": "recent-asset",
            "templateId": 122301,
            "name": "印花 | B8 (Holo) | Cologne 2026",
            "marketHashName": "Sticker | B8 (Holo) | Cologne 2026",
            "assetBuyPrice": "24.10",
            "AssetAddTime": "2026-06-07 10:00:00",
        }
    )
    old_inventory = normalize_inventory_item(
        {
            "steamAssetId": "old-asset",
            "templateId": 122302,
            "name": "印花 | MOUZ (Holo) | Cologne 2026",
            "marketHashName": "Sticker | MOUZ (Holo) | Cologne 2026",
            "assetBuyPrice": "20.00",
            "AssetAddTime": "2026-06-05 10:00:00",
        }
    )
    assert recent_inventory is not None and old_inventory is not None
    recent_events = merge_recent_purchase_events(
        [
            {
                "asset_id": "sold-asset",
                "template_id": 122303,
                "name": "印花 | 已卖出样本",
                "market_hash_name": "Sticker | Sold Sample (Holo) | Cologne 2026",
                "buy_price": "10.50",
                "acquired_at": "2026-06-07 09:00:00",
            }
        ],
        purchase_events_from_inventory([recent_inventory, old_inventory, recent_inventory]),
        lookback_hours=24,
        now=datetime(2026, 6, 7, 12, 0, 0),
    )
    recent_spend, recent_count = purchase_events_spend(recent_events)
    assert recent_count == 2
    assert recent_spend == Decimal("34.60"), recent_events
    day_start = risk_engine.purchase_window_start(
        window_mode="calendar_day",
        now=datetime(2026, 6, 7, 12, 0, 0),
    )
    assert day_start == datetime(2026, 6, 7, 0, 0, 0)
    today_events = merge_recent_purchase_events(
        [
            {
                "asset_id": "yesterday-asset",
                "template_id": 122304,
                "name": "Sticker | Yesterday (Holo) | Cologne 2026",
                "market_hash_name": "Sticker | Yesterday (Holo) | Cologne 2026",
                "buy_price": "99.90",
                "acquired_at": "2026-06-06 23:59:59",
            }
        ],
        purchase_events_from_inventory([recent_inventory]),
        lookback_hours=24,
        now=datetime(2026, 6, 7, 12, 0, 0),
        since=day_start,
    )
    today_spend, today_count = purchase_events_spend(today_events)
    assert today_count == 1
    assert today_spend == Decimal("24.10"), today_events
    fill_detected_at = datetime.now().replace(microsecond=0)
    fill_asset_add_time = fill_detected_at + timedelta(minutes=5)
    missing_price_inventory = normalize_inventory_item(
        {
            "steamAssetId": "filled-asset",
            "templateId": 122301,
            "name": "印花 | B8 (Holo) | Cologne 2026",
            "marketHashName": "Sticker | B8 (Holo) | Cologne 2026",
            "AssetAddTime": fill_asset_add_time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    assert missing_price_inventory is not None and missing_price_inventory.buy_price is None
    fill_state = {
        "purchase_price_fill_events": [
            {
                "source": "purchase_order_fill",
                "order_no": "order-1",
                "template_id": 122301,
                "name": "印花 | B8 (Holo) | Cologne 2026",
                "buy_price": "27.60",
                "quantity": 1,
                "matched_quantity": 0,
                "detected_at": fill_detected_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
        ],
        "last_inventory_asset_ids": [],
    }
    filled_inventory = apply_inferred_buy_prices_to_inventory(
        [missing_price_inventory],
        {"buy_price_fill": {"enabled": True, "match_window_minutes": 180, "lookback_hours": 48}},
        state=fill_state,
    )
    assert filled_inventory[0].buy_price == Decimal("27.60")
    assert filled_inventory[0].raw.get("_inferred_buy_price") == "27.60"
    assert fill_state["purchase_price_fill_events"][0]["matched_quantity"] == 1
    exit_rule = {"market_hash_name": inventory.market_hash_name, "expected_price": 30, "action_mode": "bid_or_sale"}
    snapshot = MarketSnapshot(
        template_id=inventory.template_id,
        name=inventory.name,
        market_hash_name=inventory.market_hash_name,
        lowest_sell_price=Decimal("31"),
        highest_purchase_price=Decimal("30"),
        on_sale_count=10,
        raw={},
    )
    tradable_inventory = InventoryItem(
        asset_id=inventory.asset_id,
        template_id=inventory.template_id,
        name=inventory.name,
        market_hash_name=inventory.market_hash_name,
        asset_status=0,
        tradable=True,
        cooldown_desc="",
        buy_price=inventory.buy_price,
        reference_price=inventory.reference_price,
        price_desc=inventory.price_desc,
        raw=inventory.raw,
    )
    exit_action = make_exit_action(tradable_inventory, exit_rule, snapshot, {"exit_strategy": {"round_places": 1}})
    assert exit_action.action == "drop_to_bid"
    assert exit_action.action_price == Decimal("30.0")
    plugin_exit_config = {
        "strategy_plugins": {"directories": ["strategies"]},
        "inventory_market": {"request_interval_seconds": 0},
        "exit_strategy": {
            "round_places": 1,
            "plugins": [
                {
                    "mode": "plugin:cs2_2026_cologne_profit_exit",
                    "enabled": True,
                    "keywords": ["Cologne 2026"],
                    "finish": "Holo",
                    "min_profit_ratio": 0.5,
                    "price_minus": 0.1,
                    "target_active_items_per_template": 3,
                    "max_reconcile_actions_per_cycle": 30,
                }
            ],
        },
    }
    plugin_item_b8 = replace(
        tradable_inventory,
        asset_id="9001",
        template_id=122378,
        name="Sticker | B8 (Holo) | Cologne 2026",
        market_hash_name="Sticker | B8 (Holo) | Cologne 2026",
        buy_price=Decimal("20.00"),
    )
    plugin_item_big = replace(
        tradable_inventory,
        asset_id="9101",
        template_id=122459,
        name="Sticker | BIG (Holo) | Cologne 2026",
        market_hash_name="Sticker | BIG (Holo) | Cologne 2026",
        buy_price=Decimal("23.20"),
    )
    plugin_snapshot_b8 = MarketSnapshot(
        template_id=122378,
        name=plugin_item_b8.name,
        market_hash_name=plugin_item_b8.market_hash_name,
        lowest_sell_price=Decimal("31.00"),
        highest_purchase_price=Decimal("28.00"),
        on_sale_count=10,
        raw={},
    )
    plugin_snapshot_big = MarketSnapshot(
        template_id=122459,
        name=plugin_item_big.name,
        market_hash_name=plugin_item_big.market_hash_name,
        lowest_sell_price=Decimal("42.00"),
        highest_purchase_price=Decimal("35.00"),
        on_sale_count=10,
        raw={},
    )
    plugin_spec = exit_engine.plugin_specs(plugin_exit_config)[0]
    plugin_builder = exit_engine.load_plugin_builder(plugin_spec, plugin_exit_config)
    plugin_inventory = [
        replace(plugin_item_b8, asset_id="9001"),
        replace(plugin_item_b8, asset_id="9002"),
        replace(plugin_item_b8, asset_id="9003"),
        replace(plugin_item_b8, asset_id="9004"),
        replace(plugin_item_big, asset_id="9101"),
        replace(plugin_item_big, asset_id="9102"),
    ]
    plugin_actions = plugin_builder(
        None,
        plugin_inventory,
        plugin_exit_config,
        {
            "plugin_config": plugin_spec,
            "build_market_snapshot": lambda active_client, active_item: plugin_snapshot_big
            if active_item.template_id == 122459
            else plugin_snapshot_b8,
            "fetch_template_sale_book": lambda active_client, template_id, **kwargs: [
                {
                    "Id": f"market-{template_id}",
                    "Price": "42.00" if template_id == 122459 else "31.00",
                    "IsMine": False,
                }
            ],
            "snapshots": {},
            "remaining_actions": 30,
        },
    )
    plugin_action_counts: dict[int | None, int] = {}
    for action in plugin_actions:
        plugin_action_counts[action.item.template_id] = plugin_action_counts.get(action.item.template_id, 0) + 1
    assert len(plugin_actions) == 5
    assert plugin_action_counts[122378] == 3
    assert plugin_action_counts[122459] == 2
    assert all(action.action == "list_sale" for action in plugin_actions)
    assert any(action.action_price == Decimal("30.9") for action in plugin_actions)
    allocation_config = {
        "quantity_strategy": {
            "mode": "equal_budget",
            "budget": 1000,
            "min_quantity_per_item": 1,
            "max_quantity_per_item": 999999,
        }
    }
    prices = [Decimal("10"), Decimal("20"), Decimal("30"), Decimal("40"), Decimal("50"), Decimal("60"), Decimal("70"), Decimal("80"), Decimal("90"), Decimal("100")]
    quantities = allocate_quantities(prices, allocation_config)
    total = sum((price * Decimal(qty) for price, qty in zip(prices, quantities)), Decimal("0"))
    assert len(quantities) == 10
    assert all(qty >= 1 for qty in quantities)
    assert total <= Decimal("1000"), total
    cleanup_template = StickerTemplate(
        template_id=122365,
        name="Sticker | 9z Team (Holo) | Cologne 2026",
        hash_name="Sticker | 9z Team (Holo) | Cologne 2026",
        lowest_sell_price=Decimal("32.4"),
        on_sale_count=56,
        raw={},
    )
    cleanup_order = ExistingPurchaseOrder(
        order_no="cleanup-order-1",
        template_id=122365,
        commodity_name=cleanup_template.name,
        purchase_price=Decimal("28.2"),
        max_purchase_price=Decimal("28.2"),
        purchase_num=5,
        supply_quantity=3,
        rank=2,
        raw={},
    )
    cleanup_action = PlanAction(
        action="keep",
        template=cleanup_template,
        bid_price=Decimal("28.2"),
        purchase_num=5,
        reference_price=Decimal("32.4"),
        existing_order=cleanup_order,
        reason="self-test",
    )
    reset_cleanup = build_cleanup_plan(
        [cleanup_action],
        {122365: cleanup_order},
        {
            "event": "Cologne 2026",
            "finish": "Holo",
            "cleanup_strategy": {
                "enabled": True,
                "reset_target_orders_each_cycle": True,
                "reset_only_partially_filled": False,
                "max_delete_per_cycle": 20,
            },
        },
    )
    assert len(reset_cleanup) == 1 and reset_cleanup[0].reason.startswith("reset_target_order_each_cycle")
    partial_only_cleanup = build_cleanup_plan(
        [cleanup_action],
        {122365: replace(cleanup_order, supply_quantity=0)},
        {
            "event": "Cologne 2026",
            "finish": "Holo",
            "cleanup_strategy": {
                "enabled": True,
                "reset_target_orders_each_cycle": True,
                "reset_only_partially_filled": True,
                "max_delete_per_cycle": 20,
            },
        },
    )
    assert partial_only_cleanup == []
    log("self-test passed")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="悠悠有品 Major 全息战队贴纸自动求购助手")
    parser.add_argument("--config", default="config.local.json", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只运行一轮")
    parser.add_argument("--loop", action="store_true", help="按 poll_seconds 循环运行")
    parser.add_argument("--live", action="store_true", help="允许真实创建/更新求购单；仍要求配置 dry_run=false")
    parser.add_argument("--self-test", action="store_true", help="运行内置离线自检")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.self_test:
        self_test()
        return 0

    config = load_config(Path(args.config))
    token = os.getenv("YOUPIN_TOKEN", "").strip()
    device_token = os.getenv("YOUPIN_DEVICE_TOKEN", "").strip()
    cookie = os.getenv("YOUPIN_COOKIE", "").strip()
    uk = os.getenv("YOUPIN_UK", "").strip()
    device_uk = os.getenv("YOUPIN_DEVICE_UK", device_token).strip()
    device_id = os.getenv("YOUPIN_PC_UUID", "").strip()
    client = YouPinClient(
        config["api_base"],
        token=token,
        device_token=device_token,
        cookie=cookie,
        uk=uk,
        device_uk=device_uk,
        device_id=device_id,
    )

    if args.live and config.get("dry_run", True):
        raise YouPinError("你加了 --live，但配置 dry_run 仍是 true。请先预览无误后再改配置。")
    if not args.loop and not args.once:
        args.once = True

    while True:
        run_cycle(client, config, live=args.live)
        if args.once:
            break
        sleep_seconds = refresh_interval_seconds(config)
        log(f"等待 {sleep_seconds} 秒后进入下一轮，约每天刷新 {round(86400 / sleep_seconds, 2)} 次")
        time.sleep(sleep_seconds)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        log("收到 Ctrl+C，已停止。")
        raise SystemExit(130)
    except YouPinError as exc:
        log(f"错误：{exc}")
        raise SystemExit(1)
