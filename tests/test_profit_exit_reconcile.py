from __future__ import annotations

import unittest
from decimal import Decimal

from strategies.cs2_2026_cologne_profit_exit import build_category_reconciliation_actions
from youpin_api import InventoryItem
from youpin_major_bidder import execute_exit_actions


SPEC = {
    "min_profit_ratio": 0.5,
    "price_minus": 0.1,
    "target_active_items_per_template": 3,
    "round_places": 1,
    "min_list_price": 0.1,
}


def inventory_item(
    asset_id: str,
    *,
    buy_price: str = "50",
    commodity_id: str = "",
    listed: bool = False,
) -> InventoryItem:
    return InventoryItem(
        asset_id=asset_id,
        template_id=122378,
        name="印花 | B8 (全息) | 2026年科隆锦标赛",
        market_hash_name="Sticker | B8 (Holo) | Cologne 2026",
        asset_status=3 if listed else 0,
        tradable=True,
        cooldown_desc="上架中饰品不可重复上架" if listed else "",
        buy_price=Decimal(buy_price),
        reference_price=Decimal("100"),
        price_desc="",
        raw={"CommodityId": commodity_id} if commodity_id else {},
    )


def sale_book(items: list[InventoryItem], own_price: str, competitor_price: str = "100") -> list[dict]:
    records = [
        {"Id": item.commodity_id, "Price": own_price, "IsMine": True}
        for item in items
        if item.is_on_sale
    ]
    records.append({"Id": "competitor", "Price": competitor_price, "IsMine": False})
    return records


class ProfitExitReconciliationTests(unittest.TestCase):
    def test_empty_category_lists_exactly_three(self) -> None:
        items = [inventory_item(str(index)) for index in range(5)]
        actions = build_category_reconciliation_actions(items, sale_book(items, "99.9"), SPEC)
        self.assertEqual([action.action for action in actions], ["list_sale"] * 3)
        self.assertTrue(all(action.action_price == Decimal("99.9") for action in actions))

    def test_two_correct_listings_only_fill_one(self) -> None:
        listed = [
            inventory_item("1", commodity_id="101", listed=True),
            inventory_item("2", commodity_id="102", listed=True),
        ]
        items = listed + [inventory_item("3"), inventory_item("4")]
        actions = build_category_reconciliation_actions(items, sale_book(listed, "99.9"), SPEC)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "list_sale")

    def test_three_correct_listings_need_no_action(self) -> None:
        listed = [
            inventory_item(str(index), commodity_id=f"10{index}", listed=True)
            for index in range(1, 4)
        ]
        actions = build_category_reconciliation_actions(listed, sale_book(listed, "99.9"), SPEC)
        self.assertEqual(actions, [])

    def test_wrong_prices_are_delisted_and_relisted(self) -> None:
        listed = [
            inventory_item(str(index), commodity_id=f"20{index}", listed=True)
            for index in range(1, 4)
        ]
        actions = build_category_reconciliation_actions(listed, sale_book(listed, "95.0"), SPEC)
        self.assertEqual([action.action for action in actions], ["relist_sale"] * 3)
        self.assertTrue(all(action.action_price == Decimal("99.9") for action in actions))

    def test_excess_correct_listings_are_reduced_to_three(self) -> None:
        listed = [
            inventory_item(str(index), commodity_id=f"30{index}", listed=True)
            for index in range(1, 6)
        ]
        actions = build_category_reconciliation_actions(listed, sale_book(listed, "99.9"), SPEC)
        self.assertEqual([action.action for action in actions], ["delist_sale"] * 2)

    def test_profit_below_fifty_percent_delists_without_relisting(self) -> None:
        listed = [
            inventory_item(str(index), buy_price="80", commodity_id=f"40{index}", listed=True)
            for index in range(1, 4)
        ]
        actions = build_category_reconciliation_actions(listed, sale_book(listed, "99.9"), SPEC)
        self.assertEqual([action.action for action in actions], ["delist_sale"] * 3)

    def test_executor_delists_before_relisting(self) -> None:
        listed = [
            inventory_item(str(index), commodity_id=f"50{index}", listed=True)
            for index in range(1, 4)
        ]
        actions = build_category_reconciliation_actions(listed, sale_book(listed, "95.0"), SPEC)

        class FakeClient:
            def __init__(self) -> None:
                self.events: list[tuple[str, object]] = []

            def off_shelf_commodity(self, commodity_id: str) -> dict:
                self.events.append(("off", commodity_id))
                return {"Code": 0}

            def get_inventory_v3(self, page_index: int, page_size: int, refresh: bool) -> dict:
                self.events.append(("inventory", None))
                return {
                    "Code": 0,
                    "Data": {
                        "ItemsInfos": [
                            {"SteamAssetId": item.asset_id, "AssetStatus": 0}
                            for item in listed
                        ]
                    },
                }

            def sell_inventory_items(self, item_infos: list[dict]) -> dict:
                self.events.append(("sell", item_infos))
                return {"Code": 0, "Data": [{"AssetId": row["AssetId"], "Status": 1} for row in item_infos]}

        client = FakeClient()
        submitted = execute_exit_actions(
            client,
            actions,
            {
                "dry_run": False,
                "exit_strategy": {
                    "enabled": True,
                    "max_actions_per_cycle": 20,
                    "off_shelf_interval_seconds": 0,
                    "relist_wait_seconds": 0,
                    "relist_poll_seconds": 0.2,
                },
            },
            live=True,
        )
        self.assertEqual(len(submitted), 3)
        self.assertEqual([event[0] for event in client.events[:3]], ["off", "off", "off"])
        self.assertEqual(client.events[-1][0], "sell")
        self.assertEqual(len(client.events[-1][1]), 3)

    def test_executor_acknowledges_only_known_withdrawal_notice(self) -> None:
        item = inventory_item("61")
        action = build_category_reconciliation_actions(
            [item],
            [{"Id": "competitor", "Price": "100", "IsMine": False}],
            SPEC,
        )[0]

        class ReminderClient:
            def __init__(self) -> None:
                self.sell_calls = 0
                self.acknowledged: list[str] = []

            def sell_inventory_items(self, item_infos: list[dict]) -> dict:
                self.sell_calls += 1
                if self.sell_calls == 1:
                    return {
                        "code": 7000002,
                        "msg": "成功",
                        "errorData": {
                            "sceneCode": "known-scene",
                            "sceneName": "出售场景交易撤回提示",
                            "countdown": 0,
                        },
                    }
                return {"Code": 0, "Data": [{"AssetId": item_infos[0]["AssetId"], "Status": 1}]}

            def acknowledge_global_reminder(self, scene_code: str) -> dict:
                self.acknowledged.append(scene_code)
                return {"code": 0, "msg": "成功"}

        client = ReminderClient()
        submitted = execute_exit_actions(
            client,
            [action],
            {
                "dry_run": False,
                "exit_strategy": {
                    "enabled": True,
                    "max_actions_per_cycle": 20,
                    "auto_acknowledge_trade_withdrawal_notice": True,
                },
            },
            live=True,
        )
        self.assertEqual(len(submitted), 1)
        self.assertEqual(client.sell_calls, 2)
        self.assertEqual(client.acknowledged, ["known-scene"])


if __name__ == "__main__":
    unittest.main()
