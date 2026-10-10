from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_API_BASE = "https://api.youpin898.com"
DEFAULT_EVENT = "Cologne 2026"
DEFAULT_FINISH = "Holo"
DEFAULT_EMULATOR_COMMAND_FILE = "state/emulator_commands.json"
DEFAULT_EMULATOR_STATUS_FILE = "state/emulator_status.json"


def load_json_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"config root must be an object: {path}")
    return payload


def ensure_section(config: dict[str, Any], key: str) -> dict[str, Any]:
    section = config.setdefault(key, {})
    if not isinstance(section, dict):
        section = {}
        config[key] = section
    return section


def apply_defaults(config: dict[str, Any]) -> dict[str, Any]:
    config.setdefault("api_base", DEFAULT_API_BASE)
    config.setdefault("event", DEFAULT_EVENT)
    config.setdefault("finish", DEFAULT_FINISH)
    config.setdefault("top_n", 10)
    config.setdefault("purchase_num", 1)
    config.setdefault("refreshes_per_day", 8)
    config.setdefault("poll_seconds", 0)

    inventory = ensure_section(config, "inventory")
    inventory.setdefault("page_size", 50)
    inventory.setdefault("max_pages", 20)
    inventory.setdefault("use_v3", True)

    purchase_risk = ensure_section(config, "purchase_risk")
    purchase_risk.setdefault("enabled", True)
    purchase_risk.setdefault("rolling_success_limit", 1000.0)
    purchase_risk.setdefault("lookback_hours", 24)
    purchase_risk.setdefault("window_mode", "rolling")
    purchase_risk.setdefault("day_start_hour", 0)
    purchase_risk.setdefault("include_active_order_exposure", True)
    purchase_risk.setdefault("active_order_exposure_multiplier", 2.0)
    purchase_risk.setdefault("fail_if_inventory_unavailable", True)

    buy_price_fill = ensure_section(config, "buy_price_fill")
    buy_price_fill.setdefault("enabled", True)
    buy_price_fill.setdefault("lookback_hours", 48)
    buy_price_fill.setdefault("match_window_minutes", 180)
    buy_price_fill.setdefault("max_pending_events", 500)
    buy_price_fill.setdefault("emulator_auto_queue", False)
    buy_price_fill.setdefault("emulator_auto_process", False)
    buy_price_fill.setdefault("emulator_auto_submit", False)
    buy_price_fill.setdefault("emulator_process_limit", 3)
    buy_price_fill.setdefault("stickers_only", True)
    buy_price_fill.setdefault("keywords", [config.get("event", DEFAULT_EVENT)])
    buy_price_fill.setdefault("finish", config.get("finish", DEFAULT_FINISH))

    daily_report = ensure_section(config, "daily_report")
    daily_report.setdefault("enabled", False)
    daily_report.setdefault("email_to", "")
    daily_report.setdefault("lookback_hours", 24)
    daily_report.setdefault("scheduled_hour", 0)
    daily_report.setdefault("scheduled_minute", 0)
    daily_report.setdefault("delivery_window_minutes", 90)
    daily_report.setdefault("send_missed", False)
    daily_report.setdefault("state_file", "state/daily_report_state.json")
    daily_report.setdefault("report_file", "state/daily_report_last.json")
    daily_report.setdefault("subject_prefix", "YouPin daily report")
    daily_report.setdefault("stickers_only", True)
    daily_report.setdefault("keywords", [config.get("event", DEFAULT_EVENT)])
    daily_report.setdefault("finish", config.get("finish", DEFAULT_FINISH))
    daily_report.setdefault("smtp_host", "")
    daily_report.setdefault("smtp_port", 465)
    daily_report.setdefault("smtp_ssl", True)
    daily_report.setdefault("smtp_starttls", False)
    daily_report.setdefault("smtp_user_env", "SMTP_USER")
    daily_report.setdefault("smtp_password_env", "SMTP_PASSWORD")
    daily_report.setdefault("smtp_from", "")

    price_alert = ensure_section(config, "price_alert")
    price_alert.setdefault("enabled", False)
    price_alert.setdefault("email_to", daily_report.get("email_to", ""))
    price_alert.setdefault("poll_seconds", 2700)
    price_alert.setdefault("state_file", "state/price_alert_state.json")
    price_alert.setdefault("subject_prefix", "YouPin price alert")
    price_alert.setdefault("rules", [])

    cleanup_strategy = ensure_section(config, "cleanup_strategy")
    cleanup_strategy.setdefault("enabled", True)
    cleanup_strategy.setdefault("auto_execute", True)
    cleanup_strategy.setdefault("scope", "strategy_targets_only")
    cleanup_strategy.setdefault("max_delete_per_cycle", 20)
    cleanup_strategy.setdefault("reset_target_orders_each_cycle", False)
    cleanup_strategy.setdefault("reset_only_partially_filled", False)
    cleanup_strategy.setdefault("event", config.get("event", ""))
    cleanup_strategy.setdefault("finish", config.get("finish", ""))
    cleanup_strategy.setdefault("preserve_template_ids", [])
    cleanup_strategy.setdefault("preserve_order_nos", [])
    cleanup_strategy.setdefault("preserve_keywords", [])

    cs_update_guard = ensure_section(config, "cs_update_guard")
    cs_update_guard.setdefault("enabled", False)
    cs_update_guard.setdefault("auto_cancel", True)
    cs_update_guard.setdefault("pause_bidding_on_update", True)
    cs_update_guard.setdefault("email_alert", True)
    cs_update_guard.setdefault("email_to", daily_report.get("email_to", ""))
    cs_update_guard.setdefault("email_subject_prefix", "CS2 update alert")
    cs_update_guard.setdefault("appid", 730)
    cs_update_guard.setdefault("poll_seconds", 60)
    cs_update_guard.setdefault("news_count", 10)
    cs_update_guard.setdefault("maxlength", 500)
    cs_update_guard.setdefault("state_file", "state/cs_update_guard_state.json")
    cs_update_guard.setdefault("title_keywords", ["Counter-Strike 2 Update", "Release Notes"])

    exit_strategy = ensure_section(config, "exit_strategy")
    exit_strategy.setdefault("enabled", True)
    exit_strategy.setdefault("auto_execute", False)
    exit_strategy.setdefault("max_actions_per_cycle", 5)
    exit_strategy.setdefault("round_places", 1)
    exit_strategy.setdefault("tick", 0.1)
    exit_strategy.setdefault("list_price_mode", "target")
    exit_strategy.setdefault("rules", [])

    bid_strategy = ensure_section(config, "bid_strategy")
    bid_strategy.setdefault("max_daily_spend", 1000.0)
    bid_strategy.setdefault("modify_interval_seconds", 60)
    bid_strategy.setdefault("round_places", 1)
    bid_strategy.setdefault("tick", 0.1)
    bid_strategy.setdefault("min_unit_price", 0.1)
    return config


def load_config(path: Path) -> dict[str, Any]:
    return apply_defaults(load_json_config(path))


def apply_desktop_runtime_settings(
    config: dict[str, Any],
    settings: dict[str, Any],
    *,
    profile: dict[str, Any] | None = None,
    selection_mode_map: dict[str, str] | None = None,
    bid_mode_map: dict[str, str] | None = None,
    create_channel_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    profile = profile or {}
    selection_mode_map = selection_mode_map or {}
    bid_mode_map = bid_mode_map or {}
    create_channel_map = create_channel_map or {}

    budget = settings["budget"]
    refreshes = int(settings["refreshes_per_day"])
    max_qty = int(settings["max_quantity_per_item"])
    active_multiplier = settings["active_order_exposure_multiplier"]
    target_rank = int(settings["target_rank"])
    modify_interval = int(settings["modify_interval_seconds"])
    report_hour, report_minute = settings["daily_report_time"]

    config["dry_run"] = bool(settings["dry_run"])
    config["refreshes_per_day"] = refreshes
    config["poll_seconds"] = 0
    if profile.get("top_n"):
        config["top_n"] = int(profile["top_n"])

    selection_strategy = ensure_section(config, "selection_strategy")
    selection_mode = str(settings["selection_mode"])
    selection_strategy["mode"] = selection_mode_map.get(selection_mode, selection_mode)

    bid_strategy = ensure_section(config, "bid_strategy")
    bid_mode = str(settings["bid_mode"])
    bid_strategy["mode"] = bid_mode_map.get(bid_mode, bid_mode)
    bid_strategy["target_rank"] = target_rank
    bid_strategy["modify_interval_seconds"] = modify_interval
    bid_strategy["allow_create_orders"] = bool(settings["allow_create_orders"])
    bid_strategy["tick"] = 0.1
    bid_strategy["min_unit_price"] = 0.1
    bid_strategy["round_places"] = 1
    bid_strategy["max_cycle_spend"] = float(budget)
    bid_strategy["max_daily_spend"] = float(budget)

    quantity_strategy = ensure_section(config, "quantity_strategy")
    quantity_strategy["mode"] = settings.get("quantity_mode") or "equal_budget"
    quantity_strategy["budget"] = float(budget)
    profile_min_qty = int(profile.get("min_quantity_per_item", 1) or 1)
    quantity_strategy["min_quantity_per_item"] = max(
        int(quantity_strategy.get("min_quantity_per_item", profile_min_qty) or profile_min_qty),
        profile_min_qty,
    )
    quantity_strategy["max_quantity_per_item"] = max_qty

    purchase_risk = ensure_section(config, "purchase_risk")
    purchase_risk["enabled"] = True
    purchase_risk["rolling_success_limit"] = float(budget)
    purchase_risk["lookback_hours"] = 24
    purchase_risk["window_mode"] = "rolling"
    purchase_risk["day_start_hour"] = 0
    purchase_risk["include_active_order_exposure"] = True
    purchase_risk["active_order_exposure_multiplier"] = float(active_multiplier)
    purchase_risk.pop("active_order_exposure_limit", None)
    purchase_risk["fail_if_inventory_unavailable"] = True

    buy_price_fill = ensure_section(config, "buy_price_fill")
    buy_price_fill["enabled"] = True
    buy_price_fill.setdefault("lookback_hours", 48)
    buy_price_fill.setdefault("match_window_minutes", 180)
    buy_price_fill.setdefault("max_pending_events", 500)
    buy_price_fill["emulator_auto_queue"] = False
    buy_price_fill["emulator_auto_process"] = False
    buy_price_fill["emulator_auto_submit"] = False
    buy_price_fill.setdefault("stickers_only", True)
    buy_price_fill.setdefault("keywords", [str(config.get("event", DEFAULT_EVENT))])
    buy_price_fill.setdefault("finish", str(config.get("finish", DEFAULT_FINISH)))

    report = ensure_section(config, "daily_report")
    report["enabled"] = bool(settings["daily_report_enabled"])
    report["email_to"] = str(settings.get("daily_report_email", "")).strip()
    report["lookback_hours"] = 24
    report["scheduled_hour"] = int(report_hour)
    report["scheduled_minute"] = int(report_minute)
    report.setdefault("delivery_window_minutes", 90)
    report.setdefault("send_missed", False)
    report.setdefault("state_file", "state/daily_report_state.json")
    report.setdefault("report_file", "state/daily_report_last.json")
    report.setdefault("subject_prefix", "YouPin daily report")
    report.setdefault("stickers_only", True)
    report.setdefault("keywords", [str(config.get("event", DEFAULT_EVENT))])
    report.setdefault("finish", str(config.get("finish", DEFAULT_FINISH)))
    report.setdefault("smtp_host", "")
    report.setdefault("smtp_port", 465)
    report.setdefault("smtp_ssl", True)
    report.setdefault("smtp_starttls", False)
    report.setdefault("smtp_user_env", "SMTP_USER")
    report.setdefault("smtp_password_env", "SMTP_PASSWORD")
    report.setdefault("smtp_from", "")

    price_alert = ensure_section(config, "price_alert")
    price_alert["enabled"] = bool(settings["price_alert_enabled"])
    price_alert["email_to"] = str(settings.get("daily_report_email") or report.get("email_to", "")).strip()
    price_alert["poll_seconds"] = 2700
    price_alert.setdefault("state_file", "state/price_alert_state.json")
    price_alert.setdefault("subject_prefix", "YouPin price alert")
    if not isinstance(price_alert.get("rules"), list):
        price_alert["rules"] = []

    cleanup_strategy = ensure_section(config, "cleanup_strategy")
    cleanup_strategy["enabled"] = True
    cleanup_strategy["auto_execute"] = bool(settings["cleanup_auto_execute"])
    cleanup_strategy.setdefault("scope", "strategy_targets_only")
    cleanup_strategy.setdefault("max_delete_per_cycle", 20)
    cleanup_strategy["event"] = str(config.get("event", DEFAULT_EVENT))
    cleanup_strategy["finish"] = str(config.get("finish", DEFAULT_FINISH))
    cleanup_strategy.setdefault("preserve_template_ids", [])
    cleanup_strategy.setdefault("preserve_order_nos", [])
    cleanup_strategy.setdefault("preserve_keywords", [])

    cs_update_guard = ensure_section(config, "cs_update_guard")
    cs_update_guard["enabled"] = bool(settings["cs_update_guard_enabled"])
    cs_update_guard.setdefault("auto_cancel", True)
    cs_update_guard.setdefault("pause_bidding_on_update", True)
    cs_update_guard.setdefault("email_alert", True)
    cs_update_guard.setdefault("email_to", report.get("email_to", ""))
    cs_update_guard.setdefault("email_subject_prefix", "CS2 update alert")
    cs_update_guard.setdefault("appid", 730)
    cs_update_guard.setdefault("poll_seconds", 60)
    cs_update_guard.setdefault("news_count", 10)
    cs_update_guard.setdefault("maxlength", 500)
    cs_update_guard.setdefault("state_file", "state/cs_update_guard_state.json")
    cs_update_guard.setdefault("title_keywords", ["Counter-Strike 2 Update", "Release Notes"])

    exit_strategy = ensure_section(config, "exit_strategy")
    exit_strategy.setdefault("enabled", True)
    exit_strategy["auto_execute"] = bool(settings["exit_auto_execute"])
    exit_strategy.setdefault("max_actions_per_cycle", 5)
    exit_strategy.setdefault("round_places", 1)
    exit_strategy.setdefault("tick", 0.1)
    exit_strategy.setdefault("list_price_mode", "target")
    exit_strategy.setdefault("rules", [])

    execution = ensure_section(config, "execution")
    create_channel = str(settings["create_order_channel"])
    execution["create_order_channel"] = create_channel_map.get(create_channel, create_channel)
    execution.setdefault("emulator_command_file", DEFAULT_EMULATOR_COMMAND_FILE)
    execution.setdefault("emulator_status_file", DEFAULT_EMULATOR_STATUS_FILE)
    execution.setdefault("emulator_auto_process_create_orders", True)
    execution.setdefault("emulator_auto_submit_create_orders", True)
    execution.setdefault("emulator_create_process_limit", 10)
    return config
