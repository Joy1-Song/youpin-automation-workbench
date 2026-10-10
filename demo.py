"""Offline portfolio walkthrough. Synthetic inputs; no API, ADB or SMTP calls."""
from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from strategies import cs2_2026_low_price_bid as strategy
from strategies.cs2_2026_cologne_profit_exit import build_category_reconciliation_actions
from youpin_api import ExistingPurchaseOrder, InventoryItem, StickerTemplate
from youpin_execution_bridge import load_command_file, queue_create_commands
from youpin_major_bidder import build_plan
from youpin_order_reconciler import build_cleanup_plan
from youpin_risk_manager import build_purchase_risk_snapshot, enforce_budget


ROOT = Path(__file__).resolve().parent


def demo_config() -> dict:
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    config["strategy_plugins"]["directories"] = [str(ROOT / "strategies")]
    return config


def synthetic_candidates() -> list[StickerTemplate]:
    return [
        StickerTemplate(
            template_id=900000 + index,
            name=f"Sticker | Demo Team {index:02d} (Holo) | Cologne 2026",
            hash_name=f"Sticker | Demo Team {index:02d} (Holo) | Cologne 2026",
            lowest_sell_price=Decimal(10 + index),
            on_sale_count=20,
            raw={},
        )
        for index in range(12)
    ]


class SyntheticMarket:
    def __init__(self, candidates: list[StickerTemplate]):
        self.prices = {item.template_id: item.lowest_sell_price for item in candidates}

    def get_template_purchase_orders(self, template_id: int) -> dict:
        price = self.prices[template_id] - Decimal("1.0")
        return {"Data": [{"Id": "demo-bid", "PurchasePrice": str(price)}]}


def synthetic_inventory() -> list[InventoryItem]:
    return [
        InventoryItem(
            asset_id=f"demo-asset-{index}", template_id=900000,
            name="Sticker | Demo Team 00 (Holo) | Cologne 2026",
            market_hash_name="Sticker | Demo Team 00 (Holo) | Cologne 2026",
            asset_status=0, tradable=True, cooldown_desc="",
            buy_price=Decimal("10"), reference_price=Decimal("20"), price_desc="",
            raw={"AssetAddTime": "2026-10-11 09:00:00"},
        )
        for index in range(5)
    ]


def run_demo() -> dict:
    config = demo_config()
    candidates = synthetic_candidates()
    market = SyntheticMarket(candidates)
    selected = strategy.select_candidates(list(reversed(candidates)), config)
    partial = ExistingPurchaseOrder(
        order_no="demo-partial-order", template_id=selected[0].template_id,
        commodity_name=selected[0].name, purchase_price=Decimal("8"),
        max_purchase_price=Decimal("9"), purchase_num=5, supply_quantity=3,
        rank=4, raw={},
    )
    initial_plan = build_plan(market, selected, {partial.template_id: partial}, config)
    cleanup = build_cleanup_plan(initial_plan, {partial.template_id: partial}, config)
    # Simulate a confirmed cancellation, then rebuild with no remaining orders.
    actions = build_plan(market, selected, {}, config)
    inventory = synthetic_inventory()
    risk = build_purchase_risk_snapshot(
        market, config, actions=actions,
        inventory_fetcher=lambda *_: inventory, checked_at="2026-10-11 12:00:00",
    )
    enforce_budget(actions, config, {"purchase_risk": risk.to_dict()}, live=False)
    with TemporaryDirectory(prefix="youpin-demo-") as directory:
        queue = Path(directory) / "commands.json"
        queue_create_commands(actions, queue, source="offline_demo")
        queue_create_commands(actions, queue, source="offline_demo")
        queued = len(load_command_file(queue)["commands"])

    book = [{"Id": "demo-competitor", "Price": "20", "IsMine": False}]
    spec = config["exit_strategy"]["plugins"][0]
    new_listings = build_category_reconciliation_actions(inventory, book, spec)
    listed = [replace(item, asset_status=3, raw={"CommodityId": f"demo-sale-{index}"})
              for index, item in enumerate(inventory[:3])]
    own_book = [{"Id": item.commodity_id, "Price": "19.9", "IsMine": True} for item in listed]
    reconciled = build_category_reconciliation_actions(listed + inventory[3:], book + own_book, spec)
    return {
        "mode": "OFFLINE / SYNTHETIC / NO TRADES",
        "candidates": len(candidates),
        "selected_categories": len(selected),
        "partial_orders_to_cancel": len(cleanup),
        "buy_plan": [{"name": action.template.name, "price": str(action.bid_price),
                      "quantity": action.purchase_num} for action in actions],
        "plan_amount": str(sum((action.spend for action in actions), Decimal("0"))),
        "filled_today": str(risk.recent_success_spend),
        "projected_risk": str(risk.projected_total_risk),
        "commands_after_two_identical_plans": queued,
        "first_listing_actions": len(new_listings),
        "actions_when_three_correct_listings_exist": len(reconciled),
    }


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=True, indent=2))
