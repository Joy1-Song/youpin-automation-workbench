from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from demo import demo_config, run_demo, synthetic_candidates, synthetic_inventory, SyntheticMarket
from strategies import cs2_2026_low_price_bid as strategy
from strategies.cs2_2026_cologne_profit_exit import build_category_reconciliation_actions
from youpin_api import MarketSnapshot, YouPinError
from youpin_builtin_strategies import StrategyError
from youpin_execution_bridge import (
    CommandFileError, cancel_stale_create_commands, load_command_file,
    queue_create_commands, write_command_file,
)
from youpin_major_bidder import build_plan
from youpin_price_alert import evaluate_rule
from youpin_risk_manager import (
    build_purchase_risk_snapshot, enforce_budget, merge_recent_purchase_events,
    purchase_events_spend, purchase_window_start,
)


def test_offline_end_to_end():
    report = run_demo()
    assert report["selected_categories"] == 10
    assert report["partial_orders_to_cancel"] == 1
    assert {row["quantity"] for row in report["buy_plan"]} == {5}
    assert report["commands_after_two_identical_plans"] == 10
    assert report["first_listing_actions"] == 3
    assert report["actions_when_three_correct_listings_exist"] == 0
    assert Decimal(report["projected_risk"]) <= Decimal("1000")


def test_selection_is_cheapest_ten_and_filters_unavailable():
    candidates = synthetic_candidates()
    candidates.append(replace(candidates[0], template_id=800000, lowest_sell_price=None))
    candidates.append(replace(candidates[0], template_id=800001, on_sale_count=0))
    selected = strategy.select_candidates(list(reversed(candidates)), demo_config())
    assert [item.template_id for item in selected] == list(range(900000, 900010))


@pytest.mark.parametrize("budget", ["1", "89.9", "100", "500", "1000"])
def test_allocation_never_exceeds_budget_or_quantity_constraints(budget):
    config = demo_config()
    config["quantity_strategy"]["budget"] = budget
    prices = [Decimal("9.9"), Decimal("35.1"), Decimal("97")]
    quantities = strategy.allocate_quantities(prices, config)
    assert all(q == 0 or 3 <= q <= 5 for q in quantities)
    assert sum((p * q for p, q in zip(prices, quantities)), Decimal("0")) <= Decimal(budget)


def test_strategy_rejects_minimum_above_hard_quantity_limit():
    config = demo_config()
    config["quantity_strategy"]["min_quantity_per_item"] = 8
    with pytest.raises(StrategyError, match="hard limit"):
        strategy.allocate_quantities([Decimal("1")], config)


def test_calendar_day_starts_at_midnight_not_previous_24_hours():
    now = datetime(2026, 10, 11, 13)
    assert purchase_window_start(window_mode="calendar_day", now=now) == datetime(2026, 10, 11)
    assert purchase_window_start(window_mode="rolling", now=now) == datetime(2026, 10, 10, 13)


def test_purchase_ledger_deduplicates_assets_and_excludes_yesterday():
    old = {"asset_id": "old", "buy_price": "20", "acquired_at": "2026-10-10 23:59:59"}
    today = {"asset_id": "new", "buy_price": "30.1", "acquired_at": "2026-10-11 00:00:00"}
    merged = merge_recent_purchase_events([old, today], [dict(today)], since=datetime(2026, 10, 11))
    assert purchase_events_spend(merged) == (Decimal("30.1"), 1)


def test_risk_rejects_budget_with_outstanding_exposure():
    config = demo_config()
    candidates = synthetic_candidates()[:1]
    actions = build_plan(SyntheticMarket(candidates), candidates, {}, config)
    inventory = [replace(synthetic_inventory()[0], buy_price=Decimal("980"))]
    risk = build_purchase_risk_snapshot(
        None, config, actions=actions, inventory_fetcher=lambda *_: inventory,
        checked_at="2026-10-11 12:00:00",
    )
    assert risk.projected_total_risk == Decimal("1025")
    with pytest.raises(YouPinError):
        enforce_budget(actions, config, {"purchase_risk": risk.to_dict()}, live=True)


def test_risk_stops_when_inventory_cannot_be_read():
    def unavailable(*_):
        raise RuntimeError("synthetic timeout")

    with pytest.raises(YouPinError):
        build_purchase_risk_snapshot(None, demo_config(), inventory_fetcher=unavailable)


@pytest.mark.parametrize("bad_data", ['{"commands":', '[]', '{}', '{"commands": {}}', '{"commands": [null]}'])
def test_corrupt_queue_fails_closed_and_preserves_file(tmp_path, bad_data):
    path = tmp_path / "commands.json"
    path.write_text(bad_data, encoding="utf-8")
    with pytest.raises(CommandFileError):
        load_command_file(path)
    assert path.read_text(encoding="utf-8") == bad_data


def test_failed_atomic_write_preserves_original_queue(tmp_path):
    path = tmp_path / "commands.json"
    write_command_file({"commands": []}, path)
    original = path.read_bytes()
    with patch("youpin_execution_bridge.os.replace", side_effect=OSError("synthetic failure")):
        with pytest.raises(OSError):
            write_command_file({"commands": [{"id": "new"}]}, path)
    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_queue_deduplicates_and_cancels_obsolete_pending_commands(tmp_path):
    config = demo_config()
    candidates = synthetic_candidates()[:2]
    actions = build_plan(SyntheticMarket(candidates), candidates, {}, config)
    path = tmp_path / "commands.json"
    queue_create_commands(actions, path)
    queue_create_commands(actions, path)
    assert len(load_command_file(path)["commands"]) == 2
    cancelled = cancel_stale_create_commands(actions[:1], path)
    assert len(cancelled) == 1
    assert cancelled[0]["template_id"] == actions[1].template.template_id
    assert cancelled[0]["status"] == "cancelled"


@pytest.mark.parametrize("kind", ["missing_cost", "cooldown"])
def test_exit_skips_unpriced_or_locked_inventory(kind):
    item = synthetic_inventory()[0]
    item = replace(item, buy_price=None) if kind == "missing_cost" else replace(item, tradable=False)
    assert build_category_reconciliation_actions(
        [item], [{"Id": "competitor", "Price": "20"}], demo_config()["exit_strategy"]["plugins"][0],
    ) == []


def test_bid_and_sale_alerts_are_independent():
    inventory = synthetic_inventory()
    snapshot = MarketSnapshot(900000, inventory[0].name, inventory[0].market_hash_name,
                              Decimal("20"), Decimal("18"), 10, {})
    rule = {"template_id": 900000, "bid_target_price": "18", "sale_target_price": "21"}
    alerts = evaluate_rule(rule, inventory, snapshot)
    assert len(alerts) == 1
    assert alerts[0]["target_price"] == "18.00"


def test_public_configuration_does_not_enable_mutations():
    config = demo_config()
    assert config["dry_run"] is True
    assert config["purchase_automation"]["enabled"] is False
    for section, keys in {
        "execution": ["emulator_auto_process_create_orders", "emulator_auto_submit_create_orders"],
        "buy_price_fill": ["emulator_auto_queue", "emulator_auto_process", "emulator_auto_submit"],
        "cleanup_strategy": ["auto_execute"], "exit_strategy": ["auto_execute"],
        "daily_report": ["enabled"], "price_alert": ["enabled"],
        "cs_update_guard": ["auto_cancel", "email_alert"],
    }.items():
        assert all(config[section][key] is False for key in keys)


def test_publication_scanner_accepts_empty_env_and_detects_filled_secret():
    from scripts.check_publication import SECRET_PATTERNS

    pattern = SECRET_PATTERNS["filled environment credential"]
    assert pattern.search("YOUPIN_TOKEN=\nYOUPIN_UK=\nSMTP_PASSWORD=\n") is None
    assert pattern.search("SMTP_PASSWORD=" + "synthetic-example-secret") is not None
