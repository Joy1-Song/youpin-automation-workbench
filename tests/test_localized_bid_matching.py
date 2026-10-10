from decimal import Decimal

from youpin_api import ExistingPurchaseOrder, StickerTemplate
from youpin_major_bidder import is_target_team_sticker
from youpin_order_reconciler import cleanup_strategy_config, order_matches_cleanup_scope


CONFIG = {
    "event": "Cologne 2026",
    "event_aliases": ["Cologne 2026", "2026年科隆锦标赛"],
    "finish": "Holo",
    "finish_aliases": ["Holo", "全息"],
    "team_names": ["paiN Gaming"],
    "cleanup_strategy": {
        "event": "Cologne 2026",
        "finish": "Holo",
    },
}


def test_chinese_localized_team_sticker_matches_bid_scope() -> None:
    template = StickerTemplate(
        template_id=122438,
        name="印花 | paiN Gaming (全息) | 2026年科隆锦标赛",
        hash_name="Sticker | paiN Gaming (Holo) | Cologne 2026",
        lowest_sell_price=Decimal("29.7"),
        on_sale_count=27,
        raw={},
    )

    assert is_target_team_sticker(template, CONFIG)


def test_chinese_localized_order_matches_cleanup_scope() -> None:
    order = ExistingPurchaseOrder(
        order_no="test-order",
        template_id=122438,
        commodity_name="印花 | paiN Gaming (全息) | 2026年科隆锦标赛",
        purchase_price=Decimal("25.0"),
        max_purchase_price=Decimal("25.1"),
        purchase_num=5,
        supply_quantity=0,
        rank=2,
        raw={},
    )

    assert order_matches_cleanup_scope(order, cleanup_strategy_config(CONFIG))
