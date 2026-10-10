from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from youpin_api import (
    CleanupAction,
    ExistingPurchaseOrder,
    InventoryItem,
    PlanAction,
    PurchaseRiskSnapshot,
    YouPinError,
    pick,
    to_decimal,
)
from youpin_strategy_sdk import quantity_budget


InventoryFetcher = Callable[[Any, dict[str, Any], dict[str, Any] | None], list[InventoryItem]]


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def inventory_add_time(item: InventoryItem) -> datetime | None:
    return parse_youpin_datetime(
        pick(
            item.raw,
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


def purchase_event_from_inventory(item: InventoryItem) -> dict[str, Any] | None:
    acquired_at = inventory_add_time(item)
    if acquired_at is None or item.buy_price is None:
        return None
    return {
        "asset_id": item.asset_id,
        "template_id": item.template_id,
        "name": item.name,
        "market_hash_name": item.market_hash_name,
        "buy_price": str(item.buy_price),
        "acquired_at": acquired_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def purchase_event_key(event: dict[str, Any]) -> str:
    asset_id = str(event.get("asset_id") or "").strip()
    if asset_id:
        return f"asset:{asset_id}"
    return "|".join(
        str(event.get(key) or "")
        for key in ("template_id", "market_hash_name", "name", "buy_price", "acquired_at")
    )


def purchase_event_time(event: dict[str, Any]) -> datetime | None:
    return parse_youpin_datetime(event.get("acquired_at"))


def purchase_event_spend(event: dict[str, Any]) -> Decimal | None:
    return to_decimal(event.get("buy_price"))


def purchase_events_from_inventory(inventory: list[InventoryItem]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in inventory:
        event = purchase_event_from_inventory(item)
        if event is not None:
            events.append(event)
    return events


def purchase_events_from_fill_events(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw_events = (state or {}).get("purchase_price_fill_events", [])
    if not isinstance(raw_events, list):
        return []
    events: list[dict[str, Any]] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        buy_price = purchase_event_spend({"buy_price": raw.get("buy_price")})
        acquired_at = parse_youpin_datetime(raw.get("detected_at"))
        if buy_price is None or acquired_at is None:
            continue
        matched_assets: list[str] = []
        seen_assets: set[str] = set()
        if isinstance(raw.get("matched_assets"), list):
            for asset_id in raw["matched_assets"]:
                asset_text = str(asset_id or "").strip()
                if asset_text and asset_text not in seen_assets:
                    seen_assets.add(asset_text)
                    matched_assets.append(asset_text)

        if matched_assets:
            event_ids = matched_assets
        else:
            try:
                matched_quantity = max(0, int(raw.get("matched_quantity") or 0))
            except (TypeError, ValueError):
                matched_quantity = 0
            if matched_quantity <= 0:
                continue
            base = str(raw.get("event_key") or raw.get("order_no") or raw.get("detected_at") or len(events))
            event_ids = [f"fill:{base}:{idx}" for idx in range(matched_quantity)]

        for asset_id in event_ids:
            events.append(
                {
                    "asset_id": asset_id,
                    "template_id": raw.get("template_id"),
                    "name": raw.get("name", ""),
                    "market_hash_name": raw.get("market_hash_name", ""),
                    "buy_price": str(buy_price),
                    "acquired_at": acquired_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "purchase_order_fill",
                    "order_no": raw.get("order_no", ""),
                }
            )
    return events


def purchase_window_start(
    *,
    window_mode: str = "rolling",
    lookback_hours: int = 24,
    day_start_hour: int = 0,
    now: datetime | None = None,
) -> datetime:
    current = now or datetime.now()
    mode = str(window_mode or "rolling").strip().lower()
    if mode in {"calendar_day", "daily", "day", "midnight"}:
        hour = min(23, max(0, int(day_start_hour)))
        start = current.replace(hour=hour, minute=0, second=0, microsecond=0)
        if current < start:
            start -= timedelta(days=1)
        return start
    return current - timedelta(hours=max(1, int(lookback_hours)))


def merge_recent_purchase_events(
    previous_events: list[dict[str, Any]],
    inventory_events: list[dict[str, Any]],
    *,
    lookback_hours: int = 24,
    now: datetime | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    cutoff = since or purchase_window_start(lookback_hours=lookback_hours, now=now)
    merged: dict[str, dict[str, Any]] = {}
    for event in [*previous_events, *inventory_events]:
        if not isinstance(event, dict):
            continue
        acquired_at = purchase_event_time(event)
        spend = purchase_event_spend(event)
        if acquired_at is None or acquired_at < cutoff or spend is None:
            continue
        normalized = dict(event)
        normalized["buy_price"] = str(spend)
        normalized["acquired_at"] = acquired_at.strftime("%Y-%m-%d %H:%M:%S")
        merged[purchase_event_key(normalized)] = normalized
    return sorted(merged.values(), key=lambda event: str(event.get("acquired_at") or ""), reverse=True)


def purchase_events_spend(events: list[dict[str, Any]]) -> tuple[Decimal, int]:
    total = Decimal("0")
    count = 0
    for event in events:
        spend = purchase_event_spend(event)
        if spend is None:
            continue
        total += spend
        count += 1
    return total, count


def recent_success_purchase_spend(
    inventory: list[InventoryItem],
    *,
    lookback_hours: int = 24,
    now: datetime | None = None,
) -> tuple[Decimal, int]:
    events = merge_recent_purchase_events(
        [],
        purchase_events_from_inventory(inventory),
        lookback_hours=lookback_hours,
        now=now,
    )
    return purchase_events_spend(events)


def purchase_risk_config(config: dict[str, Any]) -> dict[str, Any]:
    risk = config.get("purchase_risk", {})
    if not isinstance(risk, dict):
        risk = {}
    strategy = config.get("bid_strategy", {})
    quantity = config.get("quantity_strategy", {})
    default_limit = quantity.get("budget", strategy.get("max_daily_spend", "1000"))
    rolling_limit = to_decimal(risk.get("rolling_success_limit", default_limit)) or Decimal("0")
    active_multiplier = to_decimal(risk.get("active_order_exposure_multiplier", "2")) or Decimal("0")
    raw_active_limit = risk.get("active_order_exposure_limit")
    active_limit = to_decimal(raw_active_limit)
    if active_limit is None:
        active_limit = rolling_limit * active_multiplier
    return {
        "enabled": bool(risk.get("enabled", True)),
        "rolling_success_limit": rolling_limit,
        "lookback_hours": int(risk.get("lookback_hours", 24)),
        "window_mode": str(risk.get("window_mode", "rolling") or "rolling"),
        "day_start_hour": min(23, max(0, int(risk.get("day_start_hour", 0)))),
        "include_active_order_exposure": bool(risk.get("include_active_order_exposure", False)),
        "active_order_exposure_limit": active_limit,
        "active_order_exposure_multiplier": active_multiplier,
        "fail_if_inventory_unavailable": bool(risk.get("fail_if_inventory_unavailable", True)),
    }


def active_order_spend(orders: dict[int, ExistingPurchaseOrder]) -> Decimal:
    return sum((order.spend for order in orders.values()), Decimal("0"))


def action_new_exposure(action: PlanAction) -> Decimal:
    if action.action == "keep":
        return Decimal("0")
    if action.existing_order is None:
        return action.spend
    return max(action.spend - action.existing_order.spend, Decimal("0"))


def actionable_spend(actions: list[PlanAction]) -> Decimal:
    return sum((action_new_exposure(action) for action in actions), Decimal("0"))


def planned_active_spend(actions: list[PlanAction]) -> Decimal:
    return sum((action.spend for action in actions), Decimal("0"))


def active_order_spend_after_plan(
    actions: list[PlanAction],
    existing_orders: dict[int, ExistingPurchaseOrder],
    cleanup_actions: list[CleanupAction] | None = None,
) -> Decimal:
    planned_template_ids = {action.template.template_id for action in actions}
    cleanup_order_nos = {action.order.order_no for action in cleanup_actions or []}
    planned = planned_active_spend(actions)
    untouched = sum(
        (
            order.spend
            for template_id, order in existing_orders.items()
            if template_id not in planned_template_ids and order.order_no not in cleanup_order_nos
        ),
        Decimal("0"),
    )
    return planned + untouched


def build_purchase_risk_snapshot(
    client: Any,
    config: dict[str, Any],
    existing_orders: dict[int, ExistingPurchaseOrder] | None = None,
    actions: list[PlanAction] | None = None,
    cleanup_actions: list[CleanupAction] | None = None,
    state: dict[str, Any] | None = None,
    *,
    inventory_fetcher: InventoryFetcher | None = None,
    checked_at: str | None = None,
) -> PurchaseRiskSnapshot:
    risk = purchase_risk_config(config)
    previous_risk = (state or {}).get("purchase_risk", {})
    previous_events = previous_risk.get("recent_purchases", []) if isinstance(previous_risk, dict) else []
    if not isinstance(previous_events, list):
        previous_events = []
    previous_events = [
        event
        for event in previous_events
        if isinstance(event, dict)
        and str(event.get("source") or "") not in {"purchase_order_fill", "purchase_price_fill_event"}
    ]
    checked_dt = parse_youpin_datetime(checked_at) if checked_at else None
    if checked_dt is None:
        checked_dt = datetime.now()
    checked = checked_at or checked_dt.strftime("%Y-%m-%d %H:%M:%S")
    window_start = purchase_window_start(
        window_mode=str(risk["window_mode"]),
        lookback_hours=int(risk["lookback_hours"]),
        day_start_hour=int(risk["day_start_hour"]),
        now=checked_dt,
    )
    orders = existing_orders or {}
    planned_after = (
        active_order_spend_after_plan(actions or [], orders, cleanup_actions)
        if actions is not None
        else active_order_spend(orders)
    )

    if not risk["enabled"]:
        return PurchaseRiskSnapshot(
            enabled=False,
            limit=Decimal("0"),
            lookback_hours=int(risk["lookback_hours"]),
            recent_success_spend=Decimal("0"),
            recent_success_count=0,
            recent_purchases=[],
            active_order_spend=active_order_spend(orders),
            active_order_spend_after_plan=planned_after,
            active_order_exposure_limit=Decimal(risk["active_order_exposure_limit"]),
            active_order_exposure_multiplier=Decimal(risk["active_order_exposure_multiplier"]),
            include_active_order_exposure=bool(risk["include_active_order_exposure"]),
            checked_at=checked,
            window_mode=str(risk["window_mode"]),
            window_start=window_start.strftime("%Y-%m-%d %H:%M:%S"),
        )

    try:
        inventory = inventory_fetcher(client, config, state) if inventory_fetcher else []
    except Exception as exc:
        if risk["fail_if_inventory_unavailable"]:
            raise YouPinError(f"无法读取库存，不能计算 24 小时成交金额上限：{exc}") from exc
        inventory = []

    recent_purchases = merge_recent_purchase_events(
        previous_events,
        [*purchase_events_from_fill_events(state), *purchase_events_from_inventory(inventory)],
        lookback_hours=int(risk["lookback_hours"]),
        now=checked_dt,
        since=window_start,
    )
    recent_spend, recent_count = purchase_events_spend(recent_purchases)
    return PurchaseRiskSnapshot(
        enabled=True,
        limit=Decimal(risk["rolling_success_limit"]),
        lookback_hours=int(risk["lookback_hours"]),
        recent_success_spend=recent_spend,
        recent_success_count=recent_count,
        recent_purchases=recent_purchases,
        active_order_spend=active_order_spend(orders),
        active_order_spend_after_plan=planned_after,
        active_order_exposure_limit=Decimal(risk["active_order_exposure_limit"]),
        active_order_exposure_multiplier=Decimal(risk["active_order_exposure_multiplier"]),
        include_active_order_exposure=bool(risk["include_active_order_exposure"]),
        checked_at=checked,
        window_mode=str(risk["window_mode"]),
        window_start=window_start.strftime("%Y-%m-%d %H:%M:%S"),
    )


def enforce_budget(actions: list[PlanAction], config: dict[str, Any], state: dict[str, Any], live: bool) -> None:
    strategy = config.get("bid_strategy", {})
    max_cycle_spend = Decimal(str(strategy.get("max_cycle_spend", "0")))
    max_daily_spend = Decimal(str(strategy.get("max_daily_spend", "0")))
    active_spend = planned_active_spend(actions)
    new_exposure = actionable_spend(actions)
    plan_budget = quantity_budget(config)
    if plan_budget > 0 and active_spend > plan_budget:
        raise YouPinError(f"计划总挂单金额 {active_spend} 超过数量分配预算 {plan_budget}")
    if max_cycle_spend > 0 and new_exposure > max_cycle_spend:
        raise YouPinError(f"本轮新增风险金额 {new_exposure} 超过 max_cycle_spend {max_cycle_spend}")
    if live and max_cycle_spend <= 0:
        raise YouPinError("实盘模式必须设置 bid_strategy.max_cycle_spend。")
    if live and max_daily_spend <= 0:
        raise YouPinError("实盘模式必须设置 bid_strategy.max_daily_spend。")
    if max_daily_spend > 0 and active_spend > max_daily_spend:
        raise YouPinError(f"计划总挂单金额 {active_spend} 超过每日上限 max_daily_spend {max_daily_spend}")

    purchase_risk = state.get("purchase_risk")
    if isinstance(purchase_risk, dict) and purchase_risk.get("enabled", False):
        limit = to_decimal(purchase_risk.get("rolling_success_limit")) or Decimal("0")
        recent = to_decimal(purchase_risk.get("recent_success_spend")) or Decimal("0")
        projected = to_decimal(purchase_risk.get("projected_total_risk")) or recent
        active_after_plan = to_decimal(purchase_risk.get("active_order_spend_after_plan")) or Decimal("0")
        active_limit = to_decimal(purchase_risk.get("active_order_exposure_limit")) or Decimal("0")
        window_start = str(purchase_risk.get("window_start") or "最近24小时")
        if limit > 0 and recent > limit:
            raise YouPinError(f"24小时求购成交金额 {recent} 已超过上限 {limit}，起算 {window_start}，停止自动求购。")
        if active_limit > 0 and active_after_plan > active_limit:
            raise YouPinError(f"活跃求购挂单金额 {active_after_plan} 超过挂单风险上限 {active_limit}，请先撤销或缩小部分求购单。")
        if limit > 0 and projected > limit:
            raise YouPinError(
                f"24小时成交 {recent} + 活跃求购风险 {projected - recent} = {projected}，"
                f"超过24小时成交上限 {limit}，起算 {window_start}"
            )
