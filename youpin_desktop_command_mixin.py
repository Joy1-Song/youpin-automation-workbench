from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import messagebox

import youpin_daily_report as daily_report
import youpin_execution_bridge as execution_bridge
import youpin_major_bidder as bidder
import youpin_market_data as market_data
from youpin_desktop_utils import SUBPROCESS_CREATIONFLAGS


class DesktopCommandMixin:
    def command_file_path(self, config: dict | None = None) -> Path:
        if config is None:
            try:
                config = self.runtime_config()
            except Exception:
                config = {}
        execution = config.get("execution", {}) if isinstance(config, dict) else {}
        return Path(str(execution.get("emulator_command_file", execution_bridge.DEFAULT_COMMAND_FILE)))
    def emulator_status_file_path(self, config: dict | None = None) -> Path:
        if config is None:
            try:
                config = self.runtime_config()
            except Exception:
                config = {}
        execution = config.get("execution", {}) if isinstance(config, dict) else {}
        return Path(str(execution.get("emulator_status_file", "state/emulator_status.json")))
    def update_emulator_status_text(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            self.emulator_status_text.set("模拟器：状态格式异常")
            return
        if payload.get("error"):
            self.emulator_status_text.set(f"模拟器：失败 | {payload.get('error')}")
            return
        device = payload.get("device") or "未连接"
        boot = payload.get("boot_completed") or "-"
        installed = "已安装" if payload.get("app_installed") else "未安装"
        app_version = payload.get("app_version_name") or "-"
        pending = payload.get("pending_commands", 0)
        updated = payload.get("updated_at") or "-"
        extra = ""
        last_result = payload.get("last_result")
        if isinstance(last_result, dict) and "prepare_first" in last_result:
            prepared = last_result.get("prepare_first", {})
            if isinstance(prepared, dict):
                extra = f" | 已预填搜索：{prepared.get('query', '-')}"
        if isinstance(last_result, dict) and "fill_first" in last_result:
            filled = last_result.get("fill_first", {})
            if isinstance(filled, dict):
                ok = "通过" if filled.get("verified") else "未通过"
                extra += (
                    f" | 已填求购：{filled.get('bid_price', '-')} x {filled.get('purchase_num', '-')} "
                    f"| 校验{ok} | 未提交"
                )
        if payload.get("warning"):
            extra += f" | 警告：{payload.get('warning')}"
        self.emulator_status_text.set(
            f"模拟器：{device} | boot={boot} | UU {installed} v{app_version} | 待执行 {pending} | 更新 {updated}{extra}"
        )
    def load_emulator_status_payload(self, config: dict | None = None) -> dict:
        status_file = self.emulator_status_file_path(config)
        if not status_file.exists():
            return {}
        return json.loads(status_file.read_text(encoding="utf-8-sig"))
    def start_emulator_task(self, args: list[str], label: str, timeout: int = 480) -> None:
        if self.worker_running:
            return
        try:
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.set_busy(True)
        self.status.set(label)
        threading.Thread(target=self.run_emulator_task, args=(args, label, config, timeout), daemon=True).start()
    def run_emulator_task(self, args: list[str], label: str, config: dict, timeout: int) -> None:
        script = Path(__file__).with_name("youpin_emulator_executor.py")
        command_file = self.command_file_path(config)
        status_file = self.emulator_status_file_path(config)
        cmd = [
            sys.executable,
            str(script),
            "--command-file",
            str(command_file),
            "--status-file",
            str(status_file),
            *args,
            "--status",
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=Path.cwd(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=SUBPROCESS_CREATIONFLAGS,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "执行器返回失败").strip()
                try:
                    self.queue.put(("emulator_status", self.load_emulator_status_payload(config)))
                except Exception:
                    pass
                raise bidder.YouPinError(detail)
            payload = self.load_emulator_status_payload(config)
            self.queue.put(("emulator_status", payload))
            self.queue.put(("log", f"{label} 完成"))
            self.queue.put(("status", f"{label} 完成"))
        except subprocess.TimeoutExpired as exc:
            self.queue.put(("error", f"{label} 超时：{exc}"))
            self.queue.put(("status", f"{label} 超时"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
            self.queue.put(("status", f"{label} 失败"))
        finally:
            self.queue.put(("busy", False))
    def emulator_check(self) -> None:
        self.start_emulator_task(["--ensure-ready", "--start-app"], "模拟器启动/检测")
    def emulator_capture(self) -> None:
        self.start_emulator_task(["--ensure-ready", "--start-app", "--capture"], "模拟器截图/状态")
    def emulator_prepare_first(self) -> None:
        self.start_emulator_task(["--ensure-ready", "--prepare-first"], "预填首条 APP 指令")
    def emulator_fill_first(self) -> None:
        self.start_emulator_task(["--ensure-ready", "--fill-first"], "填首条 APP 求购单")
    def emulator_process_create_orders(self) -> None:
        self.start_emulator_task(["--ensure-ready", "--process-create-orders", "--limit", "10"], "自动执行求购队列", timeout=1800)
    def emulator_process_buy_price_fill(self) -> None:
        self.start_emulator_task(["--ensure-ready", "--process-buy-price-fill", "--limit", "3"], "补录库存购入价", timeout=900)
    def pending_buy_price_fill_count(self, config: dict | None = None) -> int:
        payload = execution_bridge.load_command_file(self.command_file_path(config))
        commands = payload.get("commands", [])
        if not isinstance(commands, list):
            return 0
        return sum(
            1
            for command in commands
            if isinstance(command, dict)
            and command.get("type") == "fill_inventory_buy_price"
            and command.get("status") == "pending"
        )
    def one_click_buy_price_fill(self) -> None:
        if self.worker_running:
            return
        try:
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.set_busy(True)
        self.status.set("一键补录购入价")
        threading.Thread(
            target=self.run_one_click_buy_price_fill,
            args=(config, self.token.get().strip(), self.device_token.get().strip()),
            daemon=True,
        ).start()
    def run_one_click_buy_price_fill(self, config: dict, token: str, device_token: str) -> None:
        try:
            fill_config = config.setdefault("buy_price_fill", {})
            fill_config["enabled"] = True
            fill_config["emulator_auto_queue"] = True
            client = self.make_client(config, token, device_token)
            refresh_status = bidder.refresh_buy_price_fill_state(client, config)
            queued = int(refresh_status.get("queued_buy_price_fill_commands", 0) or 0)
            pending = self.pending_buy_price_fill_count(config)
            self.queue.put(("log", f"购入价补录：已刷新成交/库存，新增队列 {queued}，待处理 {pending}"))
            if pending <= 0:
                self.queue.put(("status", "没有待补录购入价"))
                return

            script = Path(__file__).with_name("youpin_emulator_executor.py")
            command_file = self.command_file_path(config)
            status_file = self.emulator_status_file_path(config)
            limit = min(max(pending, 1), 50)
            cmd = [
                sys.executable,
                str(script),
                "--command-file",
                str(command_file),
                "--status-file",
                str(status_file),
                "--ensure-ready",
                "--process-buy-price-fill",
                "--limit",
                str(limit),
                "--status",
            ]
            proc = subprocess.run(
                cmd,
                cwd=Path.cwd(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1800,
                creationflags=SUBPROCESS_CREATIONFLAGS,
            )
            if proc.returncode != 0:
                raise bidder.YouPinError((proc.stderr or proc.stdout or "补录购入价失败").strip())
            try:
                self.queue.put(("emulator_status", self.load_emulator_status_payload(config)))
            except Exception:
                pass
            self.queue.put(("commands", execution_bridge.load_command_file(command_file)))
            inventory = market_data.fetch_inventory_items(client, config)
            market = self.fetch_inventory_market_snapshots(client, inventory, config)
            self.queue.put(("inventory_with_market", (inventory, market)))
            self.queue.put(("status", "一键补录购入价完成"))
            self.queue.put(("log", f"购入价补录完成：处理上限 {limit}"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
            self.queue.put(("status", "一键补录购入价失败"))
        finally:
            self.queue.put(("busy", False))
    def send_daily_report_now(self) -> None:
        if self.worker_running:
            return
        try:
            config = self.runtime_config()
            config.setdefault("daily_report", {})["enabled"] = True
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.set_busy(True)
        self.status.set("发送日报")
        threading.Thread(
            target=self.run_send_daily_report_now,
            args=(config, self.token.get().strip(), self.device_token.get().strip()),
            daemon=True,
        ).start()
    def run_send_daily_report_now(self, config: dict, token: str, device_token: str) -> None:
        try:
            client = self.make_client(config, token, device_token)
            status = daily_report.maybe_send_daily_report(client, config, force=True)
            if status.get("sent"):
                self.queue.put(("log", f"日报已发送：{status.get('email_to')} | {status.get('item_count')} 张 | {status.get('total_spend')} 元"))
                self.queue.put(("status", "日报发送完成"))
                self.queue.put(("daily_report_status", f"日报已发送 | {status.get('item_count')} 张 | {status.get('total_spend')} 元"))
            else:
                detail = status.get("last_error") or status.get("reason") or "未发送"
                raise bidder.YouPinError(str(detail))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
            self.queue.put(("status", "日报发送失败"))
            self.queue.put(("daily_report_status", f"日报失败：{exc}"))
        finally:
            self.queue.put(("busy", False))
    def open_emulator_screenshot(self) -> None:
        path = Path("state/emulator/screenshot.png")
        if not path.exists():
            messagebox.showinfo("暂无截图", "还没有找到 state/emulator/screenshot.png，先点“截图/状态”。")
            return
        try:
            os.startfile(path.resolve())  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))
    def pending_create_order_count(self, config: dict | None = None) -> int:
        payload = execution_bridge.load_command_file(self.command_file_path(config))
        commands = payload.get("commands", [])
        if not isinstance(commands, list):
            return 0
        return sum(
            1
            for command in commands
            if isinstance(command, dict)
            and command.get("type") == "create_purchase_order"
            and command.get("status") == "pending"
        )
    def process_create_orders_inline(self, config: dict, *, live: bool) -> None:
        effective_live = live and not bool(config.get("dry_run", True))
        execution = config.get("execution", {}) if isinstance(config.get("execution", {}), dict) else {}
        if not effective_live:
            return
        if str(execution.get("create_order_channel", "api")) not in {"emulator_queue", "app_queue"}:
            return
        if not bool(execution.get("emulator_auto_process_create_orders", True)):
            return

        pending = self.pending_create_order_count(config)
        if pending <= 0:
            return

        script = Path(__file__).with_name("youpin_emulator_executor.py")
        command_file = self.command_file_path(config)
        status_file = self.emulator_status_file_path(config)
        limit = max(1, int(execution.get("emulator_create_process_limit", 10)))
        cmd = [
            sys.executable,
            str(script),
            "--command-file",
            str(command_file),
            "--status-file",
            str(status_file),
            "--ensure-ready",
            "--process-create-orders",
            "--limit",
            str(limit),
            "--status",
        ]
        self.queue.put(("log", f"开始自动执行 APP 求购队列：{min(pending, limit)} 条"))
        proc = subprocess.run(
            cmd,
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            creationflags=SUBPROCESS_CREATIONFLAGS,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "自动执行 APP 求购队列失败").strip()
            raise bidder.YouPinError(detail)
        self.queue.put(("commands", execution_bridge.load_command_file(command_file)))
        status_payload = self.load_emulator_status_payload(config)
        self.queue.put(("emulator_status", status_payload))
        result = status_payload.get("last_result", {}).get("process_create_orders", {}) if isinstance(status_payload, dict) else {}
        processed_count = result.get("processed_count", pending)
        pending_after = result.get("pending_after", self.pending_create_order_count(config))
        self.queue.put(("log", f"APP 求购队列执行完成：处理 {processed_count} 条，剩余 {pending_after} 条"))
    def refresh_app_commands(self) -> None:
        try:
            payload = execution_bridge.load_command_file(self.command_file_path())
        except Exception as exc:
            self.append_log(f"APP command refresh failed: {exc}")
            return
        self.update_commands_table(payload)
        self.status.set("APP commands refreshed")
    def queue_create_commands(self) -> None:
        if self.worker_running:
            return
        try:
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("閰嶇疆閿欒", str(exc))
            return
        self.set_busy(True)
        self.status.set("Building APP command queue")
        threading.Thread(
            target=self.run_queue_create_commands,
            args=(config, self.token.get().strip(), self.device_token.get().strip()),
            daemon=True,
        ).start()
