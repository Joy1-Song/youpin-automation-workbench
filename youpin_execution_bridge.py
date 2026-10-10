from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


DEFAULT_COMMAND_FILE = "state/emulator_commands.json"


class CommandFileError(RuntimeError):
    """Queue state is unsafe to process; preserve it for manual recovery."""


COMMAND_STATUS_PENDING = "pending"
COMMAND_STATUS_RUNNING = "running"
COMMAND_STATUS_DONE = "done"
COMMAND_STATUS_FAILED = "failed"
COMMAND_STATUS_RETRYING = "retrying"
COMMAND_STATUS_CANCELLED = "cancelled"
COMMAND_STATUS_EXPIRED = "expired"
COMMAND_STATUS_FILLED = "filled"
ACTIVE_COMMAND_STATUSES = {COMMAND_STATUS_PENDING, COMMAND_STATUS_RUNNING, COMMAND_STATUS_RETRYING}
TERMINAL_COMMAND_STATUSES = {COMMAND_STATUS_DONE, COMMAND_STATUS_CANCELLED, COMMAND_STATUS_EXPIRED}
KNOWN_COMMAND_STATUSES = {
    *ACTIVE_COMMAND_STATUSES,
    *TERMINAL_COMMAND_STATUSES,
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_FILLED,
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def decimal_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def command_status(command: dict[str, Any]) -> str:
    status = str(command.get("status", COMMAND_STATUS_PENDING) or COMMAND_STATUS_PENDING)
    return status if status in KNOWN_COMMAND_STATUSES else COMMAND_STATUS_PENDING


def command_is_active(command: dict[str, Any]) -> bool:
    return command_status(command) in ACTIVE_COMMAND_STATUSES


def command_is_terminal(command: dict[str, Any]) -> bool:
    return command_status(command) in TERMINAL_COMMAND_STATUSES


def append_status_history(
    command: dict[str, Any],
    status: str,
    *,
    reason: str = "",
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    history = command.setdefault("status_history", [])
    if not isinstance(history, list):
        history = []
        command["status_history"] = history
    entry: dict[str, Any] = {"status": status, "at": now_iso()}
    if reason:
        entry["reason"] = reason
    if error:
        entry["error"] = error
    if result is not None:
        entry["result"] = result
    history.append(entry)
    if len(history) > 50:
        del history[:-50]


def set_command_status(
    command: dict[str, Any],
    status: str,
    *,
    reason: str = "",
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    if status not in KNOWN_COMMAND_STATUSES:
        raise ValueError(f"unknown emulator command status: {status}")
    command["status"] = status
    command["updated_at"] = now_iso()
    executor = command.setdefault("executor", {})
    if isinstance(executor, dict):
        executor["state"] = status
    append_status_history(command, status, reason=reason, result=result, error=error)
    if status == COMMAND_STATUS_DONE:
        command["completed_at"] = now_iso()
    if status == COMMAND_STATUS_RUNNING:
        command["started_at"] = now_iso()
    if status == COMMAND_STATUS_FAILED:
        command["failed_at"] = now_iso()
        command["retry_count"] = int(command.get("retry_count", 0) or 0) + 1
    if result is not None:
        command["last_result"] = result
    if error:
        command["last_error"] = error
    elif status in {COMMAND_STATUS_PENDING, COMMAND_STATUS_RUNNING, COMMAND_STATUS_DONE, COMMAND_STATUS_FILLED}:
        command.pop("last_error", None)


def normalize_command(command: dict[str, Any]) -> dict[str, Any]:
    status = command_status(command)
    command["status"] = status
    command.setdefault("created_at", now_iso())
    command.setdefault("updated_at", command.get("created_at"))
    command.setdefault("retry_count", 0)
    executor = command.setdefault("executor", {})
    if isinstance(executor, dict):
        executor.setdefault("kind", "android_emulator")
        executor["state"] = status
    if not isinstance(command.get("status_history"), list):
        command["status_history"] = []
    return command


def command_key(command: dict[str, Any]) -> str:
    if str(command.get("type", "")) == "fill_inventory_buy_price":
        raw = "|".join(
            [
                str(command.get("type", "")),
                str(command.get("asset_id", "")),
                str(command.get("template_id", "")),
                str(command.get("buy_price", "")),
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    raw = "|".join(
        [
            str(command.get("type", "")),
            str(command.get("template_id", "")),
            str(command.get("bid_price", "")),
            str(command.get("purchase_num", "")),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def load_command_file(path: str | Path = DEFAULT_COMMAND_FILE) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        return {"version": 1, "updated_at": None, "commands": []}
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise CommandFileError(f"Invalid command file: {file_path}; execution stopped") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("commands"), list):
        raise CommandFileError(f"Invalid command file structure: {file_path}; execution stopped")
    payload.setdefault("version", 1)
    commands = payload["commands"]
    if any(not isinstance(command, dict) for command in commands):
        raise CommandFileError(f"Invalid command entry: {file_path}; execution stopped")
    payload["commands"] = [normalize_command(command) for command in commands]
    return payload


def write_command_file(payload: dict[str, Any], path: str | Path = DEFAULT_COMMAND_FILE) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    commands = payload.get("commands", [])
    if not isinstance(commands, list) or any(not isinstance(command, dict) for command in commands):
        raise CommandFileError(f"Refusing to write invalid commands: {file_path}")
    payload["commands"] = [normalize_command(command) for command in commands]
    payload["updated_at"] = now_iso()
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    temporary = None
    try:
        # Replace on the same filesystem so readers never see a partially written queue.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=file_path.parent,
            prefix=f".{file_path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, file_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def action_to_create_command(action: Any, source: str = "desktop") -> dict[str, Any]:
    command = {
        "id": "",
        "type": "create_purchase_order",
        "status": COMMAND_STATUS_PENDING,
        "source": source,
        "created_at": now_iso(),
        "template_id": int(action.template.template_id),
        "template_hash_name": action.template.hash_name,
        "commodity_name": action.template.name,
        "reference_price": decimal_text(action.reference_price),
        "bid_price": decimal_text(action.bid_price),
        "purchase_num": int(action.purchase_num),
        "amount": decimal_text(action.spend),
        "selection_reason": action.template.raw.get("_selection_reason", ""),
        "executor": {
            "kind": "android_emulator",
            "state": COMMAND_STATUS_PENDING,
            "note": "Use the Android emulator UU app to create this bid, then let the API updater manage it.",
        },
    }
    command["id"] = command_key(command)
    return command


def item_to_buy_price_fill_command(item: Any, source: str = "buy_price_fill") -> dict[str, Any] | None:
    raw = getattr(item, "raw", {}) or {}
    buy_price = raw.get("_inferred_buy_price") or getattr(item, "buy_price", None)
    asset_id = str(getattr(item, "asset_id", "") or "").strip()
    name = str(getattr(item, "name", "") or "").strip()
    if not buy_price or (not asset_id and not name):
        return None
    command = {
        "id": "",
        "type": "fill_inventory_buy_price",
        "status": COMMAND_STATUS_PENDING,
        "source": source,
        "created_at": now_iso(),
        "asset_id": asset_id,
        "template_id": getattr(item, "template_id", None),
        "template_hash_name": getattr(item, "market_hash_name", ""),
        "commodity_name": name,
        "buy_price": decimal_text(buy_price),
        "purchase_order_no": raw.get("_inferred_buy_price_order_no", ""),
        "inferred_source": raw.get("_inferred_buy_price_source", ""),
        "executor": {
            "kind": "android_emulator",
            "state": COMMAND_STATUS_PENDING,
            "note": "Use the Android emulator UU inventory cost page to fill this buy price.",
        },
    }
    command["id"] = command_key(command)
    return command


def create_actions_from_plan(actions: list[Any]) -> list[Any]:
    return [action for action in actions if getattr(action, "action", "") == "create"]


def queue_create_commands(
    actions: list[Any],
    path: str | Path = DEFAULT_COMMAND_FILE,
    *,
    source: str = "desktop",
) -> list[dict[str, Any]]:
    payload = load_command_file(path)
    existing = payload.get("commands", [])
    if not isinstance(existing, list):
        existing = []
    by_id = {str(command.get("id")): command for command in existing if isinstance(command, dict)}
    queued: list[dict[str, Any]] = []
    for action in create_actions_from_plan(actions):
        command = action_to_create_command(action, source=source)
        old = by_id.get(command["id"])
        if old and not command_is_terminal(old):
            old.update(
                {
                    key: command[key]
                    for key in (
                        "source",
                        "commodity_name",
                        "template_hash_name",
                        "bid_price",
                        "purchase_num",
                        "amount",
                        "reference_price",
                        "selection_reason",
                    )
                }
            )
            old["updated_at"] = now_iso()
            if command_status(old) == COMMAND_STATUS_FAILED:
                set_command_status(old, COMMAND_STATUS_RETRYING, reason="refreshed_by_current_plan")
            queued.append(old)
        else:
            by_id[command["id"]] = command
            queued.append(command)
    payload["commands"] = sorted(by_id.values(), key=lambda item: str(item.get("created_at", "")))
    write_command_file(payload, path)
    return queued


def cancel_stale_create_commands(
    actions: list[Any],
    path: str | Path = DEFAULT_COMMAND_FILE,
    *,
    reason: str = "superseded_by_current_plan",
) -> list[dict[str, Any]]:
    payload = load_command_file(path)
    existing = payload.get("commands", [])
    if not isinstance(existing, list):
        existing = []
    current_ids = {action_to_create_command(action).get("id") for action in create_actions_from_plan(actions)}
    cancelled: list[dict[str, Any]] = []
    for command in existing:
        if not isinstance(command, dict):
            continue
        if command.get("type") != "create_purchase_order" or command_status(command) != COMMAND_STATUS_PENDING:
            continue
        if command.get("id") in current_ids:
            continue
        set_command_status(command, COMMAND_STATUS_CANCELLED, reason=reason)
        command["cancel_reason"] = reason
        cancelled.append(command)
    payload["commands"] = existing
    if cancelled:
        write_command_file(payload, path)
    return cancelled


def queue_buy_price_fill_commands(
    items: list[Any],
    path: str | Path = DEFAULT_COMMAND_FILE,
    *,
    source: str = "buy_price_fill",
) -> list[dict[str, Any]]:
    payload = load_command_file(path)
    existing = payload.get("commands", [])
    if not isinstance(existing, list):
        existing = []
    by_id = {str(command.get("id")): command for command in existing if isinstance(command, dict)}
    queued: list[dict[str, Any]] = []
    for item in items:
        command = item_to_buy_price_fill_command(item, source=source)
        if command is None:
            continue
        old = by_id.get(command["id"])
        if old:
            if command_is_terminal(old):
                continue
            old.update(
                {
                    "source": command["source"],
                    "commodity_name": command["commodity_name"],
                    "template_hash_name": command["template_hash_name"],
                    "buy_price": command["buy_price"],
                    "purchase_order_no": command["purchase_order_no"],
                    "inferred_source": command["inferred_source"],
                }
            )
            old.setdefault("created_at", command["created_at"])
            old["updated_at"] = now_iso()
            if command_status(old) == COMMAND_STATUS_FAILED:
                set_command_status(old, COMMAND_STATUS_RETRYING, reason="refreshed_buy_price_fill_command")
            queued.append(old)
        else:
            by_id[command["id"]] = command
            queued.append(command)
    payload["commands"] = sorted(by_id.values(), key=lambda item: str(item.get("created_at", "")))
    write_command_file(payload, path)
    return queued


def command_counts(payload: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for command in payload.get("commands", []):
        if not isinstance(command, dict):
            continue
        status = str(command.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts
