from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sys
import time
from datetime import datetime, timedelta
from typing import Any

import youpin_major_bidder as bidder
import youpin_daily_report as daily_report
import youpin_price_alert as price_alert


STOP = False


def local_now() -> datetime:
    return datetime.now()


def iso_now() -> str:
    return local_now().isoformat(timespec="seconds")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def setup_logging(log_file: Path, verbose: bool) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("youpin_auto_runner")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if verbose:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    return logger


def write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def signal_handler(signum: int, frame: object) -> None:
    _ = signum, frame
    global STOP
    STOP = True


def patched_bidder_log(logger: logging.Logger):
    def log(message: str) -> None:
        logger.info(message)

    return log


def make_client(args: argparse.Namespace, config: dict[str, Any]) -> bidder.YouPinClient:
    load_env_file(Path(args.env))
    token = os.getenv("YOUPIN_TOKEN", "").strip()
    device_token = os.getenv("YOUPIN_DEVICE_TOKEN", "").strip()
    cookie = os.getenv("YOUPIN_COOKIE", "").strip()
    uk = os.getenv("YOUPIN_UK", "").strip()
    device_uk = os.getenv("YOUPIN_DEVICE_UK", device_token).strip()
    device_id = os.getenv("YOUPIN_PC_UUID", "").strip()
    if not token:
        raise bidder.YouPinError("YOUPIN_TOKEN is required to read YouPin market data.")
    return bidder.YouPinClient(
        config["api_base"],
        token=token,
        device_token=device_token,
        cookie=cookie,
        uk=uk,
        device_uk=device_uk,
        device_id=device_id,
    )


def process_emulator_buy_price_fill_commands(config: dict[str, Any], logger: logging.Logger) -> dict[str, Any]:
    fill = bidder.buy_price_fill_config(config)
    if not fill["enabled"]:
        return {"enabled": False, "reason": "disabled", "processed_count": 0}
    if not fill["emulator_auto_process"]:
        return {"enabled": False, "reason": "auto_process_disabled", "processed_count": 0}
    execution_config = config.get("execution", {})
    if not isinstance(execution_config, dict):
        execution_config = {}
    command_file = Path(str(execution_config.get("emulator_command_file", "state/emulator_commands.json")))

    import youpin_emulator_executor as emulator

    pending_create_orders = emulator.pending_commands(command_file, command_type="create_purchase_order")
    if pending_create_orders:
        return {
            "enabled": False,
            "reason": "create_order_pending",
            "pending_create_orders": len(pending_create_orders),
            "processed_count": 0,
        }

    pending = emulator.pending_commands(command_file, command_type="fill_inventory_buy_price")
    if not pending:
        return {"enabled": True, "pending": 0, "processed_count": 0}

    logger.info("processing %s buy-price fill emulator command(s)", min(len(pending), int(fill["emulator_process_limit"])))
    emulator.ensure_ready()
    result = emulator.process_pending_commands(
        command_file,
        Path("state/emulator"),
        command_type="fill_inventory_buy_price",
        limit=int(fill["emulator_process_limit"]),
        submit_buy_price_fill=bool(fill["emulator_auto_submit"]),
    )
    result["enabled"] = True
    return result


def command_store_commands(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        commands = payload.get("commands", [])
        return commands if isinstance(commands, list) else []
    return payload if isinstance(payload, list) else []


def active_template_ids(client: bidder.YouPinClient) -> set[int]:
    orders = bidder.fetch_existing_orders(client)
    return {int(template_id) for template_id in orders}


def action_template_id(action: Any) -> int | None:
    template = getattr(action, "template", None)
    template_id = getattr(template, "template_id", None)
    try:
        return int(template_id)
    except (TypeError, ValueError):
        return None


def restore_failed_missing_create_commands(
    command_file: Path,
    expected_actions: list[Any] | tuple[Any, ...] | None,
    live_active_template_ids: set[int],
    *,
    limit: int,
    logger: logging.Logger,
) -> list[str]:
    expected_by_template: dict[int, Any] = {}
    for action in expected_actions or []:
        if str(getattr(action, "action", "")).lower() != "create":
            continue
        template_id = action_template_id(action)
        if template_id is None or template_id in live_active_template_ids:
            continue
        expected_by_template[template_id] = action
    if not expected_by_template or not command_file.exists():
        return []

    try:
        payload = json.loads(command_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("failed to read emulator command file for create-order repair: %s", exc)
        return []

    commands = command_store_commands(payload)
    pending_templates = {
        int(command.get("template_id"))
        for command in commands
        if str(command.get("type")) == "create_purchase_order"
        and str(command.get("status")) == "pending"
        and str(command.get("template_id", "")).isdigit()
    }
    restored: list[str] = []
    now = iso_now()
    for template_id, action in expected_by_template.items():
        if len(restored) >= max(1, limit):
            break
        if template_id in pending_templates:
            continue
        failed = [
            command
            for command in commands
            if str(command.get("type")) == "create_purchase_order"
            and str(command.get("status")) == "failed"
            and str(command.get("template_id")) == str(template_id)
        ]
        if not failed:
            continue
        command = sorted(failed, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))[-1]
        template = getattr(action, "template", None)
        command.update(
            {
                "status": "pending",
                "template_hash_name": getattr(template, "hash_name", command.get("template_hash_name")),
                "commodity_name": getattr(template, "name", command.get("commodity_name")),
                "reference_price": str(getattr(action, "reference_price", command.get("reference_price"))),
                "bid_price": str(getattr(action, "bid_price", command.get("bid_price"))),
                "purchase_num": int(getattr(action, "purchase_num", command.get("purchase_num") or 0)),
                "amount": str(getattr(action, "spend", command.get("amount"))),
                "selection_reason": getattr(template, "raw", {}).get("_selection_reason", command.get("selection_reason")),
                "updated_at": now,
            }
        )
        command.pop("last_error", None)
        command.pop("completed_at", None)
        executor = command.setdefault("executor", {})
        if isinstance(executor, dict):
            executor["state"] = "pending"
        restored.append(str(command.get("id", template_id)))

    if restored:
        if isinstance(payload, dict):
            payload["updated_at"] = now
        command_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("restored %s failed create-order command(s) for missing active orders: %s", len(restored), ", ".join(restored))
    return restored


def process_emulator_create_order_commands(
    config: dict[str, Any],
    logger: logging.Logger,
    client: bidder.YouPinClient | None = None,
    expected_actions: list[Any] | tuple[Any, ...] | None = None,
) -> dict[str, Any]:
    if not bidder.purchase_automation_enabled(config):
        return {
            "enabled": False,
            "reason": "purchase_automation_paused",
            "processed_count": 0,
        }
    execution_config = config.get("execution", {})
    if not isinstance(execution_config, dict):
        execution_config = {}
    if not bool(execution_config.get("emulator_auto_process_create_orders", True)):
        return {"enabled": False, "processed_count": 0}
    command_file = Path(str(execution_config.get("emulator_command_file", "state/emulator_commands.json")))
    limit = max(1, int(execution_config.get("emulator_create_process_limit", 10)))
    submit = bool(execution_config.get("emulator_auto_submit_create_orders", True))

    import youpin_emulator_executor as emulator

    repair: dict[str, Any] = {"restored_count": 0, "processed_count": 0}
    pending = emulator.pending_commands(command_file, command_type="create_purchase_order")
    if not pending:
        if client is not None and expected_actions:
            try:
                live_active = active_template_ids(client)
                restored = restore_failed_missing_create_commands(
                    command_file,
                    expected_actions,
                    live_active,
                    limit=limit,
                    logger=logger,
                )
                repair["restored_count"] = len(restored)
                repair["restored_ids"] = restored
                pending = emulator.pending_commands(command_file, command_type="create_purchase_order")
            except Exception as exc:
                logger.exception("emulator create-order repair failed before processing: %s", exc)
                repair["last_error"] = str(exc)
        if not pending:
            return {"enabled": True, "pending": 0, "processed_count": 0, "repair": repair}

    logger.info("processing %s create-order emulator command(s)", min(len(pending), limit))
    emulator.ensure_ready()
    result = emulator.process_pending_commands(
        command_file,
        Path("state/emulator"),
        command_type="create_purchase_order",
        limit=limit,
        submit_create_order=submit,
    )
    if client is not None and expected_actions:
        try:
            live_active = active_template_ids(client)
            restored = restore_failed_missing_create_commands(
                command_file,
                expected_actions,
                live_active,
                limit=limit,
                logger=logger,
            )
            repair["restored_count"] = len(restored)
            repair["restored_ids"] = restored
            if restored:
                repair_result = emulator.process_pending_commands(
                    command_file,
                    Path("state/emulator"),
                    command_type="create_purchase_order",
                    limit=min(limit, len(restored)),
                    submit_create_order=submit,
                )
                repair["processed_count"] = repair_result.get("processed_count", 0)
                repair["processed"] = repair_result.get("processed", [])
        except Exception as exc:
            logger.exception("emulator create-order repair failed: %s", exc)
            repair["last_error"] = str(exc)
    result["repair"] = repair
    result["enabled"] = True
    return result


def process_daily_report(config: dict[str, Any], client: bidder.YouPinClient, logger: logging.Logger) -> dict[str, Any]:
    status = daily_report.maybe_send_daily_report(client, config)
    if status.get("sent"):
        logger.info(
            "daily report sent to %s; items=%s spend=%s",
            status.get("email_to"),
            status.get("item_count"),
            status.get("total_spend"),
        )
    elif status.get("last_error"):
        logger.warning("daily report failed: %s", status.get("last_error"))
    return status


def process_price_alert(config: dict[str, Any], client: bidder.YouPinClient, logger: logging.Logger) -> dict[str, Any]:
    status = price_alert.check_price_alerts(client, config)
    if status.get("sent"):
        logger.info(
            "price alert sent to %s; triggered=%s",
            status.get("email_to"),
            status.get("new_triggered_count", status.get("triggered_count")),
        )
    elif status.get("reason") not in {"disabled", "no_rules", "no_new_alerts"}:
        logger.info("price alert status: %s", status.get("reason", status))
    return status


def run_guard_check(args: argparse.Namespace, logger: logging.Logger) -> tuple[dict[str, Any], dict[str, Any]]:
    config = bidder.load_config(Path(args.config))
    client = make_client(args, config)
    old_log = bidder.log
    bidder.log = patched_bidder_log(logger)
    try:
        status = bidder.check_cs_update_guard(client, config, live=args.live)
        try:
            status["buy_price_fill"] = bidder.refresh_buy_price_fill_state(client, config)
            try:
                status["buy_price_fill"]["emulator_process"] = process_emulator_buy_price_fill_commands(config, logger)
            except Exception as exc:
                logger.exception("emulator buy price fill failed: %s", exc)
                status["buy_price_fill"]["emulator_process"] = {"enabled": True, "last_error": str(exc), "checked_at": iso_now()}
        except Exception as exc:
            logger.exception("buy price fill monitor failed: %s", exc)
            status["buy_price_fill"] = {"enabled": True, "last_error": str(exc), "checked_at": iso_now()}
        try:
            status["daily_report"] = process_daily_report(config, client, logger)
        except Exception as exc:
            logger.exception("daily report check failed: %s", exc)
            status["daily_report"] = {"enabled": True, "last_error": str(exc), "checked_at": iso_now()}
    finally:
        bidder.log = old_log
    return status, config


def run_one_cycle(args: argparse.Namespace, logger: logging.Logger) -> tuple[bool, dict[str, Any]]:
    load_env_file(Path(args.env))
    config = bidder.load_config(Path(args.config))
    effective_live = bool(args.live and not config.get("dry_run", True))
    if args.live and config.get("dry_run", True):
        raise bidder.YouPinError("args --live is set, but config dry_run is still true.")
    if effective_live and not os.getenv("YOUPIN_TOKEN", "").strip():
        raise bidder.YouPinError("YOUPIN_TOKEN is required for live automation.")

    client = make_client(args, config)
    exit_actions: list[bidder.ExitAction] = []
    submitted_exit_actions: list[bidder.ExitAction] = []
    daily_report_status: dict[str, Any] = {}
    price_alert_status: dict[str, Any] = {}
    old_log = bidder.log
    bidder.log = patched_bidder_log(logger)
    try:
        plan = bidder.run_cycle(client, config, live=args.live)
        emulator_create_order_process: dict[str, Any] = {}
        if effective_live:
            try:
                emulator_create_order_process = process_emulator_create_order_commands(
                    config,
                    logger,
                    client=client,
                    expected_actions=plan.actions,
                )
            except Exception as exc:
                logger.exception("emulator create-order processing failed: %s", exc)
                emulator_create_order_process = {"enabled": True, "last_error": str(exc), "checked_at": iso_now()}
        exit_strategy = config.get("exit_strategy", {})
        if exit_strategy.get("auto_execute", False):
            exit_actions = bidder.prepare_exit_actions(client, config)
            submitted_exit_actions = bidder.execute_exit_actions(client, exit_actions, config, live=effective_live)
        try:
            daily_report_status = process_daily_report(config, client, logger)
        except Exception as exc:
            logger.exception("daily report check failed: %s", exc)
            daily_report_status = {"enabled": True, "last_error": str(exc), "checked_at": iso_now()}
        try:
            price_alert_status = process_price_alert(config, client, logger)
        except Exception as exc:
            logger.exception("price alert check failed: %s", exc)
            price_alert_status = {"enabled": True, "last_error": str(exc), "checked_at": iso_now()}
    finally:
        bidder.log = old_log

    status = {
        "updated_at": iso_now(),
        "success": True,
        "live": args.live,
        "effective_live": effective_live,
        "dry_run": bool(config.get("dry_run", True)),
        "action_count": len(plan.actions),
        "planned_active_spend": str(plan.active_spend),
        "new_exposure": str(plan.new_exposure),
        "purchase_risk": plan.state.get("purchase_risk"),
        "purchase_automation_enabled": bidder.purchase_automation_enabled(config),
        "exit_auto_execute": bool(config.get("exit_strategy", {}).get("auto_execute", False)),
        "exit_action_count": len(exit_actions),
        "exit_actionable_count": len([action for action in exit_actions if action.is_actionable]),
        "exit_submitted_count": len(submitted_exit_actions),
        "emulator_create_order_process": emulator_create_order_process,
        "daily_report": daily_report_status,
        "price_alert": price_alert_status,
        "refresh_interval_seconds": bidder.refresh_interval_seconds(config),
        "cs_update_guard": bidder.load_cs_update_guard_state(config),
    }
    return True, status


def merge_status(path: Path, updates: dict[str, Any]) -> None:
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                payload = loaded
        except Exception:
            payload = {}
    payload.update(updates)
    write_status(path, payload)


def sleep_until(next_run: datetime, logger: logging.Logger, args: argparse.Namespace | None = None, status_path: Path | None = None) -> None:
    next_guard_at = local_now()
    next_report_check_at = local_now()
    next_price_alert_check_at = local_now()
    while not STOP:
        seconds = (next_run - local_now()).total_seconds()
        if seconds <= 0:
            return
        sleep_seconds = min(30, seconds)
        if args is not None:
            try:
                config = bidder.load_config(Path(args.config))
                guard = bidder.cs_update_guard_config(config)
                if guard["enabled"]:
                    now = local_now()
                    if now >= next_guard_at:
                        guard_status, _ = run_guard_check(args, logger)
                        if status_path is not None:
                            merge_status(
                                status_path,
                                {
                                    "updated_at": iso_now(),
                                    "cs_update_guard": guard_status,
                                    "guard_last_check_at": iso_now(),
                                },
                            )
                        next_guard_at = now + timedelta(seconds=int(guard["poll_seconds"]))
                    sleep_seconds = min(sleep_seconds, max(1, (next_guard_at - now).total_seconds()))
            except Exception as exc:
                logger.exception("CS update guard check failed: %s", exc)
                next_guard_at = local_now() + timedelta(seconds=300)
            try:
                now = local_now()
                if now >= next_report_check_at:
                    config = bidder.load_config(Path(args.config))
                    report_config = daily_report.daily_report_config(config)
                    if report_config["enabled"]:
                        client = make_client(args, config)
                        report_status = process_daily_report(config, client, logger)
                        if status_path is not None:
                            merge_status(
                                status_path,
                                {
                                    "updated_at": iso_now(),
                                    "daily_report": report_status,
                                    "daily_report_last_check_at": iso_now(),
                                },
                            )
                    next_report_check_at = now + timedelta(seconds=60)
            except Exception as exc:
                logger.exception("daily report scheduled check failed: %s", exc)
                next_report_check_at = local_now() + timedelta(seconds=300)
            try:
                now = local_now()
                if now >= next_price_alert_check_at:
                    config = bidder.load_config(Path(args.config))
                    alert_config = price_alert.price_alert_config(config)
                    if alert_config["enabled"]:
                        client = make_client(args, config)
                        alert_status = process_price_alert(config, client, logger)
                        if status_path is not None:
                            merge_status(
                                status_path,
                                {
                                    "updated_at": iso_now(),
                                    "price_alert": alert_status,
                                    "price_alert_last_check_at": iso_now(),
                                },
                            )
                    next_price_alert_check_at = now + timedelta(seconds=int(alert_config["poll_seconds"]))
                    sleep_seconds = min(sleep_seconds, max(1, (next_price_alert_check_at - now).total_seconds()))
            except Exception as exc:
                logger.exception("price alert scheduled check failed: %s", exc)
                next_price_alert_check_at = local_now() + timedelta(seconds=300)
        time.sleep(max(1, min(30, sleep_seconds)))
    logger.info("stop requested")


def run_forever(args: argparse.Namespace, logger: logging.Logger) -> int:
    status_path = Path(args.status_file)
    error_retry_seconds = max(60, int(args.error_retry_seconds))

    if args.guard_only:
        return run_guard_forever(args, logger)

    initial_delay_seconds = max(0, int(args.initial_delay_seconds))
    if initial_delay_seconds > 0 and not args.once:
        config = bidder.load_config(Path(args.config))
        next_run = local_now() + timedelta(seconds=initial_delay_seconds)
        write_status(
            status_path,
            {
                "updated_at": iso_now(),
                "success": True,
                "live": args.live,
                "dry_run": bool(config.get("dry_run", True)),
                "started_at": iso_now(),
                "finished_at": "",
                "next_run_at": next_run.isoformat(timespec="seconds"),
                "initial_delay_seconds": initial_delay_seconds,
                "last_error": "",
            },
        )
        logger.info("initial delay enabled; first cycle at %s", next_run.isoformat(timespec="seconds"))
        sleep_until(next_run, logger, args=args, status_path=status_path)

    while not STOP:
        started_at = iso_now()
        logger.info("cycle started")
        try:
            ok, status = run_one_cycle(args, logger)
            config = bidder.load_config(Path(args.config))
            interval = bidder.refresh_interval_seconds(config)
            next_run = local_now() + timedelta(seconds=interval)
            status.update(
                {
                    "started_at": started_at,
                    "finished_at": iso_now(),
                    "next_run_at": next_run.isoformat(timespec="seconds"),
                    "last_error": "",
                }
            )
            write_status(status_path, status)
            logger.info("cycle finished; next run at %s", status["next_run_at"])
            if args.once:
                return 0 if ok else 1
            sleep_until(next_run, logger, args=args, status_path=status_path)
        except Exception as exc:
            logger.exception("cycle failed: %s", exc)
            next_run = local_now() + timedelta(seconds=error_retry_seconds)
            write_status(
                status_path,
                {
                    "updated_at": iso_now(),
                    "success": False,
                    "started_at": started_at,
                    "finished_at": iso_now(),
                    "next_run_at": next_run.isoformat(timespec="seconds"),
                    "last_error": str(exc),
                },
            )
            if args.once:
                return 1
            sleep_until(next_run, logger, args=args, status_path=status_path)
    return 0


def run_guard_forever(args: argparse.Namespace, logger: logging.Logger) -> int:
    status_path = Path(args.status_file)
    while not STOP:
        started_at = iso_now()
        try:
            guard_status, config = run_guard_check(args, logger)
            guard = bidder.cs_update_guard_config(config)
            next_run = local_now() + timedelta(seconds=int(guard["poll_seconds"]))
            write_status(
                status_path,
                {
                    "updated_at": iso_now(),
                    "success": True,
                    "guard_only": True,
                    "live": args.live,
                    "dry_run": bool(config.get("dry_run", True)),
                    "started_at": started_at,
                    "finished_at": iso_now(),
                    "next_run_at": next_run.isoformat(timespec="seconds"),
                    "cs_update_guard": guard_status,
                    "last_error": "",
                },
            )
            logger.info("guard check finished; next run at %s", next_run.isoformat(timespec="seconds"))
            if args.once:
                return 0
            sleep_until(next_run, logger, args=args, status_path=status_path)
        except Exception as exc:
            logger.exception("guard check failed: %s", exc)
            next_run = local_now() + timedelta(seconds=max(60, int(args.error_retry_seconds)))
            write_status(
                status_path,
                {
                    "updated_at": iso_now(),
                    "success": False,
                    "guard_only": True,
                    "started_at": started_at,
                    "finished_at": iso_now(),
                    "next_run_at": next_run.isoformat(timespec="seconds"),
                    "last_error": str(exc),
                },
            )
            if args.once:
                return 1
            sleep_until(next_run, logger, args=args, status_path=status_path)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unattended YouPin purchase-order automation runner")
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--live", action="store_true", help="Allow real create/update calls when dry_run=false.")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument("--log-file", default="logs/auto_runner.log")
    parser.add_argument("--status-file", default="state/auto_runner_status.json")
    parser.add_argument("--error-retry-seconds", type=int, default=300)
    parser.add_argument("--guard-only", action="store_true", help="Only monitor CS2 Steam updates and cancel orders when triggered.")
    parser.add_argument("--initial-delay-seconds", type=int, default=0, help="Delay the first full cycle while keeping guard checks active.")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    logger = setup_logging(Path(args.log_file), verbose=args.verbose or args.once)
    return run_forever(args, logger)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
