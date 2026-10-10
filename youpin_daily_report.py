from __future__ import annotations

import json
import os
import smtplib
from datetime import datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import youpin_api as api
import youpin_major_bidder as bidder
import youpin_market_data as market_data
import youpin_risk_manager as risk_manager


def daily_report_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("daily_report", {})
    if not isinstance(raw, dict):
        raw = {}
    keywords = raw.get("keywords")
    if not isinstance(keywords, list):
        keywords = [config.get("event", "Cologne 2026")]
    return {
        "enabled": bool(raw.get("enabled", False)),
        "email_to": str(raw.get("email_to", raw.get("recipient_email", "")) or "").strip(),
        "lookback_hours": max(1, int(raw.get("lookback_hours", 24))),
        "scheduled_hour": min(23, max(0, int(raw.get("scheduled_hour", 0)))),
        "scheduled_minute": min(59, max(0, int(raw.get("scheduled_minute", 0)))),
        "delivery_window_minutes": max(1, int(raw.get("delivery_window_minutes", 90))),
        "send_missed": bool(raw.get("send_missed", False)),
        "state_file": str(raw.get("state_file", "state/daily_report_state.json")),
        "report_file": str(raw.get("report_file", "state/daily_report_last.json")),
        "subject_prefix": str(raw.get("subject_prefix", "YouPin daily report")),
        "stickers_only": bool(raw.get("stickers_only", True)),
        "keywords": [str(keyword).strip() for keyword in keywords if str(keyword).strip()],
        "finish": str(raw.get("finish", config.get("finish", "Holo")) or "").strip(),
        "smtp_host": str(raw.get("smtp_host", "") or os.getenv("SMTP_HOST", "")).strip(),
        "smtp_port": int(raw.get("smtp_port", os.getenv("SMTP_PORT", "465") or 465)),
        "smtp_ssl": bool(raw.get("smtp_ssl", True)),
        "smtp_starttls": bool(raw.get("smtp_starttls", False)),
        "smtp_user_env": str(raw.get("smtp_user_env", "SMTP_USER")),
        "smtp_password_env": str(raw.get("smtp_password_env", "SMTP_PASSWORD")),
        "smtp_from": str(raw.get("smtp_from", "") or os.getenv("SMTP_FROM", "")).strip(),
    }


def load_report_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_report_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def normalized(value: Any) -> str:
    return api.normalize_text(str(value or ""))


def event_allowed(event: dict[str, Any], report: dict[str, Any]) -> bool:
    text = normalized(f"{event.get('name', '')} {event.get('market_hash_name', '')}")
    if report["stickers_only"] and not any(marker in text for marker in ("sticker", "印花")):
        return False
    keywords = [normalized(keyword) for keyword in report["keywords"] if normalized(keyword)]
    if keywords and not any(keyword in text for keyword in keywords):
        return False
    finish = normalized(report["finish"])
    if finish:
        finish_markers = ("holo", "全息") if finish == "holo" else (finish,)
        if not any(marker in text for marker in finish_markers):
            return False
    return True


def money_text(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def event_time(event: dict[str, Any]) -> datetime | None:
    return risk_manager.parse_youpin_datetime(event.get("acquired_at"))


def event_price(event: dict[str, Any]) -> Decimal | None:
    return api.to_decimal(event.get("buy_price"))


def fill_events_from_state(
    state: dict[str, Any],
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> list[dict[str, Any]]:
    raw_events = state.get("purchase_price_fill_events", [])
    if not isinstance(raw_events, list):
        return []
    result: list[dict[str, Any]] = []
    emitted_assets: set[str] = set()
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        buy_price = event_price(raw)
        acquired_at = risk_manager.parse_youpin_datetime(raw.get("detected_at"))
        if buy_price is None or acquired_at is None:
            continue
        if window_start is not None and acquired_at < window_start:
            continue
        if window_end is not None and acquired_at > window_end:
            continue
        try:
            matched_quantity = max(0, int(raw.get("matched_quantity") or 0))
        except (TypeError, ValueError):
            matched_quantity = 0
        matched_assets = []
        seen_assets: set[str] = set()
        if isinstance(raw.get("matched_assets"), list):
            for asset_id in raw["matched_assets"]:
                asset_text = str(asset_id or "").strip()
                if asset_text and asset_text not in seen_assets:
                    seen_assets.add(asset_text)
                    matched_assets.append(asset_text)
        if matched_assets:
            event_ids = [asset_id for asset_id in matched_assets if asset_id not in emitted_assets]
        elif matched_quantity > 0:
            base = str(raw.get("event_key") or raw.get("order_no") or len(result))
            event_ids = [f"fill:{base}:{idx}" for idx in range(matched_quantity)]
        else:
            continue

        for asset_id in event_ids:
            emitted_assets.add(asset_id)
            result.append(
                {
                    "asset_id": asset_id,
                    "template_id": raw.get("template_id"),
                    "name": raw.get("name", ""),
                    "market_hash_name": raw.get("market_hash_name", ""),
                    "buy_price": str(buy_price),
                    "acquired_at": acquired_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "purchase_price_fill_event",
                    "order_no": raw.get("order_no", ""),
                }
            )
    return result


def group_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for event in events:
        price = event_price(event)
        if price is None:
            continue
        key = str(event.get("market_hash_name") or event.get("name") or event.get("template_id") or "unknown")
        row = grouped.setdefault(
            key,
            {
                "name": str(event.get("name") or event.get("market_hash_name") or key),
                "market_hash_name": str(event.get("market_hash_name") or ""),
                "template_id": event.get("template_id"),
                "count": 0,
                "total_spend": Decimal("0"),
                "prices": [],
            },
        )
        row["count"] += 1
        row["total_spend"] += price
        row["prices"].append(str(price))

    result: list[dict[str, Any]] = []
    for row in grouped.values():
        count = max(1, int(row["count"]))
        total = Decimal(row["total_spend"])
        result.append(
            {
                "name": row["name"],
                "market_hash_name": row["market_hash_name"],
                "template_id": row["template_id"],
                "count": count,
                "total_spend": money_text(total),
                "avg_price": money_text(total / Decimal(count)),
                "prices": row["prices"],
            }
        )
    return sorted(result, key=lambda item: (Decimal(str(item["total_spend"])), int(item["count"])), reverse=True)


def report_window(now: datetime, report: dict[str, Any], *, force: bool = False) -> tuple[datetime, datetime]:
    window_end = scheduled_time(now, report)
    if now < window_end:
        window_end -= timedelta(days=1)
    window_start = window_end - timedelta(hours=int(report["lookback_hours"]))
    return window_start, window_end


def generate_report(
    client: api.YouPinClient,
    config: dict[str, Any],
    now: datetime | None = None,
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    report = daily_report_config(config)
    if window_end is None:
        window_end = now
    if window_start is None:
        window_start = window_end - timedelta(hours=int(report["lookback_hours"]))
    state = bidder.load_state(config)
    previous_risk = state.get("purchase_risk", {})
    previous_events = previous_risk.get("recent_purchases", []) if isinstance(previous_risk, dict) else []
    if not isinstance(previous_events, list):
        previous_events = []
    previous_events = [
        event
        for event in previous_events
        if isinstance(event, dict)
        and str(event.get("source") or "") not in {"purchase_order_fill", "purchase_price_fill_event"}
    ]

    inventory = market_data.fetch_inventory_items(client, config, state=state)
    fill_events = fill_events_from_state(state, window_start=window_start, window_end=window_end)
    inventory_events = risk_manager.purchase_events_from_inventory(inventory)
    events = risk_manager.merge_recent_purchase_events(
        previous_events,
        [*fill_events, *inventory_events],
        lookback_hours=int(report["lookback_hours"]),
        now=window_end,
        since=window_start,
    )
    filtered = []
    for event in events:
        if not isinstance(event, dict) or not event_allowed(event, report):
            continue
        acquired_at = event_time(event)
        if acquired_at is None or acquired_at > window_end:
            continue
        filtered.append(event)
    total_spend, item_count = risk_manager.purchase_events_spend(filtered)
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_start": window_start.isoformat(timespec="seconds"),
        "window_end": window_end.isoformat(timespec="seconds"),
        "lookback_hours": int(report["lookback_hours"]),
        "item_count": int(item_count),
        "total_spend": money_text(total_spend),
        "groups": group_events(filtered),
        "events": filtered,
    }
    return payload


def format_report_text(payload: dict[str, Any]) -> str:
    lines = [
        "悠悠有品 24 小时成交日报",
        "",
        f"统计区间：{payload.get('window_start')} - {payload.get('window_end')}",
        f"收到贴纸：{payload.get('item_count', 0)} 张",
        f"总花费：{payload.get('total_spend', '0.00')} 元",
        "",
        "品类汇总：",
    ]
    groups = payload.get("groups", [])
    if not groups:
        lines.append("无成交。")
    else:
        for idx, item in enumerate(groups, start=1):
            lines.append(
                f"{idx:02d}. {item.get('name')} | {item.get('count')} 张 | "
                f"总额 {item.get('total_spend')} | 均价 {item.get('avg_price')}"
            )
    return "\n".join(lines)


def infer_smtp_host(address: str) -> str:
    domain = address.rsplit("@", 1)[-1].lower() if "@" in address else ""
    known = {
        "qq.com": "smtp.qq.com",
        "foxmail.com": "smtp.qq.com",
        "163.com": "smtp.163.com",
        "126.com": "smtp.126.com",
        "gmail.com": "smtp.gmail.com",
        "outlook.com": "smtp.office365.com",
        "hotmail.com": "smtp.office365.com",
    }
    return known.get(domain, "")


def smtp_settings(config: dict[str, Any]) -> dict[str, Any]:
    report = daily_report_config(config)
    user = os.getenv(report["smtp_user_env"], "").strip()
    password = os.getenv(report["smtp_password_env"], "").strip()
    from_addr = report["smtp_from"] or os.getenv("SMTP_FROM", "").strip() or user
    host = report["smtp_host"] or infer_smtp_host(from_addr or user)
    if not host or not user or not password or not from_addr:
        raise api.YouPinError("SMTP is not configured. Set SMTP_USER, SMTP_PASSWORD and SMTP_HOST or SMTP_FROM in .env.")
    return {
        "host": host,
        "port": int(report["smtp_port"]),
        "ssl": bool(report["smtp_ssl"]),
        "starttls": bool(report["smtp_starttls"]),
        "user": user,
        "password": password,
        "from_addr": from_addr,
    }


def send_text_email(*, to_addr: str, subject: str, body: str, config: dict[str, Any]) -> dict[str, Any]:
    settings = smtp_settings(config)

    message = EmailMessage()
    message["From"] = settings["from_addr"]
    message["To"] = to_addr
    message["Subject"] = subject
    message.set_content(body)

    if settings["ssl"]:
        with smtplib.SMTP_SSL(settings["host"], int(settings["port"]), timeout=30) as smtp:
            smtp.login(settings["user"], settings["password"])
            smtp.send_message(message)
    else:
        with smtplib.SMTP(settings["host"], int(settings["port"]), timeout=30) as smtp:
            if settings["starttls"]:
                smtp.starttls()
            smtp.login(settings["user"], settings["password"])
            smtp.send_message(message)
    return {"sent": True, "email_to": to_addr, "smtp_host": settings["host"]}


def send_email(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    report = daily_report_config(config)
    if not report["email_to"]:
        raise api.YouPinError("daily_report.email_to is empty.")
    return send_text_email(
        to_addr=report["email_to"],
        subject=f"{report['subject_prefix']} {datetime.now().strftime('%Y-%m-%d')}",
        body=format_report_text(payload),
        config=config,
    )


def scheduled_time(now: datetime, report: dict[str, Any]) -> datetime:
    return now.replace(
        hour=int(report["scheduled_hour"]),
        minute=int(report["scheduled_minute"]),
        second=0,
        microsecond=0,
    )


def next_report_at(now: datetime, report: dict[str, Any]) -> str:
    scheduled = scheduled_time(now, report)
    if now >= scheduled:
        scheduled += timedelta(days=1)
    return scheduled.isoformat(timespec="seconds")


def due_to_send(state: dict[str, Any], report: dict[str, Any], now: datetime) -> bool:
    report_date = now.date().isoformat()
    if state.get("last_sent_date") == report_date:
        return False
    scheduled = scheduled_time(now, report)
    if now < scheduled:
        return False
    if not report["send_missed"] and now > scheduled + timedelta(minutes=int(report["delivery_window_minutes"])):
        return False
    return True


def maybe_send_daily_report(
    client: api.YouPinClient,
    config: dict[str, Any],
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    now = now or datetime.now()
    report = daily_report_config(config)
    state_file = Path(report["state_file"])
    state = load_report_state(state_file)
    base_status = {
        "enabled": bool(report["enabled"]),
        "email_to": report["email_to"],
        "next_report_at": next_report_at(now, report),
        "sent": False,
    }
    if not report["enabled"]:
        return {**base_status, "reason": "disabled"}
    if not report["email_to"]:
        return {**base_status, "reason": "email_to_empty"}
    if not force and not due_to_send(state, report, now):
        return {**base_status, "reason": "not_due"}

    try:
        window_start, window_end = report_window(now, report, force=force)
        payload = generate_report(client, config, now=now, window_start=window_start, window_end=window_end)
        report_file = Path(report["report_file"])
        if force:
            report_file = report_file.with_name(f"{report_file.stem}_manual{report_file.suffix}")
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        email_status = send_email(payload, config)
        report_summary = {
            "item_count": payload["item_count"],
            "total_spend": payload["total_spend"],
            "report_file": str(report_file),
            "window_start": payload["window_start"],
            "window_end": payload["window_end"],
        }
        if force:
            state["last_manual_sent_at"] = now.isoformat(timespec="seconds")
            state["last_manual_error"] = ""
            state["last_manual_report"] = report_summary
        else:
            state["last_sent_date"] = now.date().isoformat()
            state["last_sent_at"] = now.isoformat(timespec="seconds")
            state["last_error"] = ""
            state["last_report"] = report_summary
        write_report_state(state_file, state)
        return {
            **base_status,
            **email_status,
            "item_count": payload["item_count"],
            "total_spend": payload["total_spend"],
            "report_file": str(report_file),
        }
    except Exception as exc:
        if force:
            state["last_manual_failed_at"] = now.isoformat(timespec="seconds")
            state["last_manual_error"] = str(exc)
        else:
            state["last_failed_date"] = now.date().isoformat()
            state["last_failed_at"] = now.isoformat(timespec="seconds")
            state["last_error"] = str(exc)
        write_report_state(state_file, state)
        return {**base_status, "sent": False, "last_error": str(exc)}
