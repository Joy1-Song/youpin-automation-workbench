from __future__ import annotations

from typing import Any

from youpin_api import CleanupAction, ExistingPurchaseOrder, PlanAction


def cleanup_strategy_config(config: dict[str, Any]) -> dict[str, Any]:
    cleanup = config.get("cleanup_strategy", {})
    if not isinstance(cleanup, dict):
        cleanup = {}
    return {
        "enabled": bool(cleanup.get("enabled", False)),
        "auto_execute": bool(cleanup.get("auto_execute", False)),
        "scope": str(cleanup.get("scope", "strategy_targets_only")),
        "max_delete_per_cycle": int(cleanup.get("max_delete_per_cycle", 20)),
        "reset_target_orders_each_cycle": bool(cleanup.get("reset_target_orders_each_cycle", False)),
        "reset_only_partially_filled": bool(cleanup.get("reset_only_partially_filled", False)),
        "event": str(cleanup.get("event", config.get("event", ""))),
        "finish": str(cleanup.get("finish", config.get("finish", ""))),
        "event_aliases": cleanup.get("event_aliases", config.get("event_aliases", [])),
        "finish_aliases": cleanup.get("finish_aliases", config.get("finish_aliases", [])),
        "preserve_template_ids": cleanup.get("preserve_template_ids", []),
        "preserve_order_nos": cleanup.get("preserve_order_nos", []),
        "preserve_keywords": cleanup.get("preserve_keywords", []),
    }


def order_matches_cleanup_scope(order: ExistingPurchaseOrder, cleanup: dict[str, Any]) -> bool:
    name = order.commodity_name or ""
    lower_name = name.lower()
    events = [
        str(value).strip().lower()
        for value in [cleanup.get("event"), *(cleanup.get("event_aliases", []) or [])]
        if str(value or "").strip()
    ]
    finishes = [
        str(value).strip().lower()
        for value in [cleanup.get("finish"), *(cleanup.get("finish_aliases", []) or [])]
        if str(value or "").strip()
    ]
    if events and not any(event in lower_name for event in events):
        return False
    if finishes:
        finish_matches = any(finish in lower_name for finish in finishes)
        if "holo" in finishes:
            finish_matches = finish_matches or "全息" in name
        if not finish_matches:
            return False
    return True


def should_reset_target_order(order: ExistingPurchaseOrder, cleanup: dict[str, Any]) -> bool:
    if not cleanup["reset_target_orders_each_cycle"]:
        return False
    if cleanup["reset_only_partially_filled"]:
        if order.purchase_num is None:
            return False
        return 0 < order.supply_quantity < order.purchase_num
    return True


def preserved_order(order: ExistingPurchaseOrder, cleanup: dict[str, Any]) -> bool:
    preserve_template_ids = {int(item) for item in cleanup["preserve_template_ids"] if str(item).strip().isdigit()}
    preserve_order_nos = {str(item) for item in cleanup["preserve_order_nos"] if str(item)}
    preserve_keywords = [str(item).lower() for item in cleanup["preserve_keywords"] if str(item).strip()]
    if order.template_id in preserve_template_ids:
        return True
    if order.order_no in preserve_order_nos:
        return True
    return bool(preserve_keywords and any(keyword in order.commodity_name.lower() for keyword in preserve_keywords))


def build_cleanup_plan(
    actions: list[PlanAction],
    existing_orders: dict[int, ExistingPurchaseOrder],
    config: dict[str, Any],
) -> list[CleanupAction]:
    cleanup = cleanup_strategy_config(config)
    if not cleanup["enabled"]:
        return []
    target_template_ids = {action.template.template_id for action in actions}
    cleanup_actions: list[CleanupAction] = []
    for template_id, order in sorted(existing_orders.items(), key=lambda item: item[1].commodity_name):
        if preserved_order(order, cleanup):
            continue
        if not order.order_no or not order_matches_cleanup_scope(order, cleanup):
            continue

        if template_id in target_template_ids:
            if not should_reset_target_order(order, cleanup):
                continue
            cleanup_actions.append(
                CleanupAction(
                    order=order,
                    reason=f"reset_target_order_each_cycle; filled={order.fill_text}",
                )
            )
            continue

        cleanup_actions.append(
            CleanupAction(
                order=order,
                reason=f"not_in_current_strategy_targets; target_count={len(target_template_ids)}",
            )
        )
    max_delete = max(0, int(cleanup["max_delete_per_cycle"]))
    if max_delete > 0:
        cleanup_actions = cleanup_actions[:max_delete]
    return cleanup_actions


def cleanup_summary(actions: list[CleanupAction]) -> dict[str, Any]:
    return {
        "count": len(actions),
        "spend": str(sum((action.spend for action in actions), start=0)),
        "orders": [
            {
                "template_id": action.order.template_id,
                "name": action.order.commodity_name,
                "order_no": action.order.order_no,
                "fill": action.order.fill_text,
                "reason": action.reason,
            }
            for action in actions
        ],
    }
