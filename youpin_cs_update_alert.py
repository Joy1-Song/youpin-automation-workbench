from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import youpin_daily_report as daily_report


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def to_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def alert_config(config: dict[str, Any], guard: dict[str, Any] | None = None) -> dict[str, Any]:
    guard = guard if isinstance(guard, dict) else config.get("cs_update_guard", {})
    if not isinstance(guard, dict):
        guard = {}
    report = config.get("daily_report", {})
    if not isinstance(report, dict):
        report = {}
    return {
        "enabled": bool(guard.get("email_alert", True)),
        "email_to": str(guard.get("email_to", report.get("email_to", "")) or "").strip(),
        "subject_prefix": str(guard.get("email_subject_prefix", "CS2 update alert")),
    }


def steam_release_time(latest: dict[str, Any]) -> str:
    try:
        return datetime.fromtimestamp(int(latest.get("date") or 0)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(latest.get("date", ""))


def format_alert_body(latest: dict[str, Any], status: dict[str, Any]) -> str:
    cancelled_spend = to_decimal(status.get("cancelled_order_spend")) or Decimal("0")
    return "\n".join(
        [
            "CS2 更新已检测到。",
            "",
            f"检测时间：{status.get('last_triggered_at') or status.get('checked_at') or now_text()}",
            f"更新标题：{latest.get('title', '')}",
            f"发布时间：{steam_release_time(latest)}",
            f"Steam gid：{latest.get('gid', '')}",
            f"链接：{latest.get('url', '')}",
            "",
            f"实盘状态：{'是' if status.get('effective_live') else '否'}",
            f"已取消求购单：{int(status.get('cancelled_order_count') or 0)}",
            f"已取消求购金额：{cancelled_spend}",
            f"策略暂停：{'是' if status.get('halted') else '否'}",
        ]
    )


def failure_status(enabled: bool, exc: Exception) -> dict[str, Any]:
    return {"enabled": bool(enabled), "sent": False, "last_error": str(exc)}


def send_alert(
    config: dict[str, Any],
    state: dict[str, Any],
    latest: dict[str, Any],
    status: dict[str, Any],
) -> dict[str, Any]:
    alert = alert_config(config)
    latest_gid = str(latest.get("gid", ""))
    if not alert["enabled"]:
        return {"enabled": False, "reason": "disabled"}
    if not alert["email_to"]:
        return {"enabled": True, "sent": False, "reason": "email_to_empty"}
    if latest_gid and str(state.get("last_email_gid", "")) == latest_gid:
        return {"enabled": True, "sent": False, "reason": "already_sent", "gid": latest_gid}

    subject = f"{alert['subject_prefix']} {latest.get('title', '')}".strip()
    result = daily_report.send_text_email(
        to_addr=alert["email_to"],
        subject=subject,
        body=format_alert_body(latest, status),
        config=config,
    )
    state["last_email_gid"] = latest_gid
    state["last_email_sent_at"] = now_text()
    state["last_email_to"] = alert["email_to"]
    state["last_email_error"] = ""
    return {"enabled": True, "gid": latest_gid, **result}
