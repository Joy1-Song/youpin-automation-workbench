from __future__ import annotations

import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tkinter import messagebox

import youpin_exit_manager as exit_manager
import youpin_major_bidder as bidder
import youpin_market_data as market_data
import youpin_price_alert as price_alert
from youpin_desktop_constants import EXIT_ACTION_LABELS, EXIT_MODE_TO_VALUE, EXIT_VALUE_TO_MODE
from youpin_desktop_utils import money_text


class DesktopInventoryMixin:
    def inventory_key(self, item: bidder.InventoryItem) -> str:
        return price_alert.item_key(item)
    def inventory_market_snapshot(self, item: bidder.InventoryItem) -> bidder.MarketSnapshot | None:
        return self.current_inventory_market_by_key.get(self.inventory_key(item))
    def exit_rule_map(self) -> dict[str, dict]:
        try:
            config = self.runtime_config()
        except Exception:
            return {}
        return exit_manager.rule_map(config)
    def exit_rule_for_item(self, item: bidder.InventoryItem) -> dict | None:
        try:
            config = self.runtime_config()
        except Exception:
            return None
        return exit_manager.rule_for_item(config, item)
    def price_alert_rule_map(self) -> dict[str, dict]:
        try:
            config = self.runtime_config()
        except Exception:
            return {}
        return price_alert.rule_map(config)
    def price_alert_rule_for_item(self, item: bidder.InventoryItem) -> dict | None:
        try:
            config = self.runtime_config()
        except Exception:
            return None
        return price_alert.rule_for_item(config, item)
    def exit_mode_label(self, value: object) -> str:
        return EXIT_VALUE_TO_MODE.get(str(value), str(value or ""))
    def update_exit_rule_summary_from_config(self) -> None:
        rule_count = len(self.exit_rule_map())
        auto_text = "开" if self.exit_auto_execute.get() else "关"
        self.exit_summary.set(f"自动卖出规则：{rule_count}    自动执行：{auto_text}")
    def money_or_dash(self, value: Decimal | None) -> str:
        if value is None:
            return "-"
        return money_text(value)
    def spread_text(self, snapshot: bidder.MarketSnapshot | None) -> str:
        if not snapshot or snapshot.lowest_sell_price is None or snapshot.highest_purchase_price is None:
            return "-"
        return money_text(snapshot.lowest_sell_price - snapshot.highest_purchase_price)
    def selected_inventory_items(self) -> list[bidder.InventoryItem]:
        items: list[bidder.InventoryItem] = []
        for iid in self.inventory_tree.selection():
            item = self.current_inventory_by_iid.get(str(iid))
            if item:
                items.append(item)
        return items
    def on_inventory_select(self, _event: tk.Event | None = None) -> None:
        selected = self.selected_inventory_items()
        if not selected:
            self.selected_inventory_text.set("未选择库存")
            self.selected_market_text.set("卖一 -    最高求购 -    成本 -    价差 -")
            return
        first = selected[0]
        snapshot = self.inventory_market_snapshot(first)
        same_count = sum(1 for item in self.current_inventory_by_iid.values() if self.inventory_key(item) == self.inventory_key(first))
        tradable_count = sum(
            1
            for item in self.current_inventory_by_iid.values()
            if self.inventory_key(item) == self.inventory_key(first) and item.status_text == "可交易"
        )
        self.selected_inventory_text.set(f"{first.name}    已选 {len(selected)}    同品 {same_count}    可交易 {tradable_count}")
        rule = self.exit_rule_for_item(first)
        if rule:
            target = exit_manager.rule_target(rule)
            if target is not None:
                self.exit_target_price.set(str(target))
            quantity = rule.get("max_quantity", "")
            if quantity:
                self.exit_rule_quantity.set(str(quantity))
            mode = exit_manager.rule_action_mode(rule)
            if mode:
                self.exit_action_mode.set(self.exit_mode_label(mode))
        else:
            self.exit_target_price.set("")
            self.exit_rule_quantity.set("1")
        alert_rule = self.price_alert_rule_for_item(first)
        if alert_rule:
            bid_price, sale_price = price_alert.rule_targets(alert_rule)
            self.price_alert_bid_price.set(str(bid_price) if bid_price is not None else "")
            self.price_alert_sale_price.set(str(sale_price) if sale_price is not None else "")
        else:
            self.price_alert_bid_price.set("")
            self.price_alert_sale_price.set("")
        self.selected_market_text.set(
            "卖一 "
            f"{self.money_or_dash(snapshot.lowest_sell_price if snapshot else None)}    "
            "最高求购 "
            f"{self.money_or_dash(snapshot.highest_purchase_price if snapshot else None)}    "
            f"成本 {self.money_or_dash(first.buy_price)}    "
            f"价差 {self.spread_text(snapshot)}"
        )
    def fill_exit_price_from_selected(self, source: str) -> None:
        selected = self.selected_inventory_items()
        if not selected:
            messagebox.showinfo("未选择库存", "请先在“库存仓库”表中选择一个库存。")
            return
        item = selected[0]
        snapshot = self.inventory_market_snapshot(item)
        value: Decimal | None = None
        if source == "sell" and snapshot:
            value = snapshot.lowest_sell_price
        elif source == "bid" and snapshot:
            value = snapshot.highest_purchase_price
        elif source == "cost_plus" and item.buy_price is not None:
            value = item.buy_price * Decimal("1.10")
        if value is None or value <= 0:
            messagebox.showinfo("暂无价格", "当前选中库存还没有可用的盘口/成本价格，请先刷新库存盘口。")
            return
        try:
            config = self.runtime_config()
            places = int(config.get("exit_strategy", {}).get("round_places", 1))
        except Exception:
            places = 1
        self.exit_target_price.set(str(bidder.money(value, places=places)))
    def save_price_alert_for_selected_inventory(self) -> None:
        selected = self.selected_inventory_items()
        if not selected:
            messagebox.showinfo("未选择库存", "请先在“库存仓库”表中选择要设置预警的库存。")
            return
        try:
            bid_target_price = price_alert.parse_optional_price(self.price_alert_bid_price.get(), "求购预警价")
            sale_target_price = price_alert.parse_optional_price(self.price_alert_sale_price.get(), "在售预警价")
            config = self.runtime_config()
            updated = price_alert.upsert_rules_for_items(
                config,
                selected,
                bid_target_price=bid_target_price,
                sale_target_price=sale_target_price,
                email_to=self.daily_report_email.get().strip() or config.get("daily_report", {}).get("email_to", ""),
                enabled=True,
                poll_seconds=price_alert.DEFAULT_POLL_SECONDS,
            )
        except Exception as exc:
            messagebox.showerror("预警规则错误", str(exc))
            return
        self.price_alert_enabled.set(True)

        try:
            Path(self.config_path.get()).write_text(bidder.json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        rules = price_alert.price_alert_config(config)["rules"]
        bid_text = price_alert.display_price(bid_target_price)
        sale_text = price_alert.display_price(sale_target_price)
        self.append_log(f"已保存 {updated} 条价格预警，求购预警 {bid_text}，在售预警 {sale_text}，检查频率 30 分钟")
        self.price_alert_status.set(f"价格预警：开 | 规则 {len(rules)} | 每 30 分钟")
        self.update_inventory_table(list(self.current_inventory_by_iid.values()))
    def delete_selected_price_alerts(self) -> None:
        selected = self.selected_inventory_items()
        if not selected:
            messagebox.showinfo("未选择预警", "请先在“库存仓库”表中选择要删除预警的品类。")
            return
        try:
            config = self.runtime_config()
            removed = price_alert.delete_rules_for_items(config, selected)
            Path(self.config_path.get()).write_text(bidder.json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        self.append_log(f"已删除 {removed} 条价格预警")
        self.update_inventory_table(list(self.current_inventory_by_iid.values()))
    def check_price_alert_now(self) -> None:
        if self.worker_running:
            return
        try:
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.set_busy(True)
        self.status.set("正在检查价格预警")
        threading.Thread(
            target=self.run_check_price_alert_now,
            args=(config, self.token.get().strip(), self.device_token.get().strip()),
            daemon=True,
        ).start()
    def selected_inventory_for_limit_order(self, max_quantity: int) -> list[bidder.InventoryItem]:
        selected = self.selected_inventory_items()
        if not selected:
            return []
        selected_keys = {self.inventory_key(item) for item in selected}
        counts: dict[str, int] = {}
        result: list[bidder.InventoryItem] = []
        for iid in self.inventory_tree.get_children():
            item = self.current_inventory_by_iid.get(str(iid))
            if not item:
                continue
            key = self.inventory_key(item)
            if key not in selected_keys or item.status_text != "可交易":
                continue
            if counts.get(key, 0) >= max_quantity:
                continue
            result.append(item)
            counts[key] = counts.get(key, 0) + 1
        return result
    def refresh_selected_inventory_market(self) -> None:
        if self.worker_running:
            return
        selected = self.selected_inventory_items()
        if not selected:
            messagebox.showinfo("未选择库存", "请先在“库存仓库”表中选择要刷新盘口的库存。")
            return
        try:
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.set_busy(True)
        self.status.set("正在刷新选中库存盘口")
        threading.Thread(
            target=self.run_refresh_selected_inventory_market,
            args=(config, self.token.get().strip(), self.device_token.get().strip(), selected),
            daemon=True,
        ).start()
    def parse_exit_target_price(self) -> Decimal:
        try:
            price = Decimal(self.exit_target_price.get())
        except InvalidOperation as exc:
            raise bidder.YouPinError("出货预期价必须是数字。") from exc
        if price <= 0:
            raise bidder.YouPinError("出货预期价必须大于 0。")
        return price
    def save_exit_rule_for_selected_inventory(self) -> None:
        selected = self.selected_inventory_items()
        if not selected:
            messagebox.showinfo("未选择库存", "请先在“库存仓库”表中选择要设置预期价的库存。")
            return
        try:
            target_price = self.parse_exit_target_price()
            max_quantity = self.parse_positive_int(self.exit_rule_quantity, "出货数量")
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("规则错误", str(exc))
            return

        action_mode = EXIT_MODE_TO_VALUE.get(self.exit_action_mode.get(), "bid_or_sale")
        updated = exit_manager.upsert_rules_for_items(
            config,
            selected,
            target_price=target_price,
            max_quantity=max_quantity,
            action_mode=action_mode,
        )

        try:
            path = Path(self.config_path.get())
            path.write_text(bidder.json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self.append_log(f"已保存 {updated} 条出货规则，预期价 {target_price}，数量 {max_quantity}")
        self.update_exit_rule_summary_from_config()
        self.update_inventory_table(list(self.current_inventory_by_iid.values()))
        messagebox.showinfo("已保存", f"已保存 {updated} 条自动卖价规则。")
    def delete_selected_exit_rules(self) -> None:
        selected = self.selected_inventory_items()
        if not selected:
            messagebox.showinfo("未选择规则", "请先在“库存仓库”表中选择要删除自动卖价的品类。")
            return

        try:
            config = self.runtime_config()
            removed = exit_manager.delete_rules_for_items(config, selected)
            Path(self.config_path.get()).write_text(bidder.json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        self.append_log(f"已删除 {removed} 条出货规则")
        self.update_exit_rule_summary_from_config()
        self.update_inventory_table(list(self.current_inventory_by_iid.values()))
    def refresh_exit_plan(self) -> None:
        if self.worker_running:
            return
        try:
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.set_busy(True)
        self.status.set("正在刷新自动卖出状态")
        threading.Thread(
            target=self.run_refresh_exit_plan,
            args=(config, self.token.get().strip(), self.device_token.get().strip()),
            daemon=True,
        ).start()
    def execute_selected_exit_actions(self) -> None:
        if self.worker_running:
            return
        if not self.live_enabled.get() or self.dry_run.get():
            messagebox.showwarning("未允许实盘", "出货会真实上架/按求购价上架，请先取消 dry_run 并勾选“允许实盘按钮”。")
            return
        selected_actions = list(self.current_exit_actions_by_iid.values())
        actionable = [action for action in selected_actions if action.is_actionable]
        if not actionable:
            messagebox.showinfo("没有可执行动作", "当前自动卖出规则里没有达标动作。")
            return
        lines = [f"{EXIT_ACTION_LABELS.get(action.action, action.action)}  {action.item.name}  {action.action_price}" for action in actionable]
        message = "将真实执行以下出货动作：\n\n" + "\n".join(lines[:8])
        if len(lines) > 8:
            message += f"\n... 另外 {len(lines) - 8} 个"
        message += "\n\n确认继续？"
        if not messagebox.askyesno("确认出货", message):
            return
        try:
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.set_busy(True)
        self.status.set("正在执行出货")
        threading.Thread(
            target=self.run_execute_exit_actions,
            args=(config, self.token.get().strip(), self.device_token.get().strip(), actionable),
            daemon=True,
        ).start()
    def execute_limit_order_for_selected_inventory(self) -> None:
        if self.worker_running:
            return
        if not self.live_enabled.get() or self.dry_run.get():
            messagebox.showwarning("未允许实盘", "限价出货会真实上架/按求购价上架，请先取消 dry_run 并勾选“允许实盘按钮”。")
            return
        try:
            target_price = self.parse_exit_target_price()
            max_quantity = self.parse_positive_int(self.exit_rule_quantity, "出货数量")
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("限价错误", str(exc))
            return

        items = self.selected_inventory_for_limit_order(max_quantity)
        if not items:
            messagebox.showinfo("没有可出货库存", "请先在“库存仓库”表中选择可交易库存，或等待 CD 结束。")
            return

        action_mode = EXIT_MODE_TO_VALUE.get(self.exit_action_mode.get(), "bid_or_sale")
        lines = [f"{item.name}  限价 {target_price}" for item in items[:8]]
        message = "将按当前限价真实出货以下库存：\n\n" + "\n".join(lines)
        if len(items) > 8:
            message += f"\n... 另外 {len(items) - 8} 个"
        message += "\n\n求购达标优先模式会在最高求购达到限价时直接丢求购，否则按限价上架。确认继续？"
        if not messagebox.askyesno("确认限价出货", message):
            return

        self.set_busy(True)
        self.status.set("正在执行限价出货")
        threading.Thread(
            target=self.run_execute_limit_inventory,
            args=(config, self.token.get().strip(), self.device_token.get().strip(), items, target_price, action_mode),
            daemon=True,
        ).start()
    def run_refresh_selected_inventory_market(
        self,
        config: dict,
        token: str,
        device_token: str,
        inventory: list[bidder.InventoryItem],
    ) -> None:
        try:
            client = self.make_client(config, token, device_token)
            market = self.fetch_inventory_market_snapshots(client, inventory, config)
            self.queue.put(("inventory_market", market))
            self.queue.put(("status", "选中库存盘口已刷新"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
            self.queue.put(("status", "刷新选中盘口失败"))
        finally:
            self.queue.put(("busy", False))
    def fetch_inventory_market_snapshots(
        self,
        client: bidder.YouPinClient,
        inventory: list[bidder.InventoryItem],
        config: dict,
    ) -> dict[str, bidder.MarketSnapshot]:
        return market_data.fetch_inventory_market_snapshots(
            client,
            inventory,
            config,
            log=lambda message: self.queue.put(("log", message)),
        )
    def run_refresh_exit_plan(self, config: dict, token: str, device_token: str) -> None:
        old_log = bidder.log

        def ui_log(message: str) -> None:
            self.queue.put(("log", message))

        bidder.log = ui_log
        try:
            client = self.make_client(config, token, device_token)
            actions = bidder.prepare_exit_actions(client, config)
            self.queue.put(("exit_actions", actions))
            self.queue.put(("status", "自动卖出状态已刷新"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
            self.queue.put(("status", "刷新自动卖出状态失败"))
        finally:
            bidder.log = old_log
            self.queue.put(("busy", False))
    def run_execute_exit_actions(
        self,
        config: dict,
        token: str,
        device_token: str,
        actions: list[bidder.ExitAction],
    ) -> None:
        old_log = bidder.log

        def ui_log(message: str) -> None:
            self.queue.put(("log", message))

        bidder.log = ui_log
        try:
            client = self.make_client(config, token, device_token)
            submitted = bidder.execute_exit_actions(client, actions, config, live=True)
            self.queue.put(("log", f"已执行 {len(submitted)} 个出货动作"))
            inventory = market_data.fetch_inventory_items(client, config)
            market = self.fetch_inventory_market_snapshots(client, inventory, config)
            self.queue.put(("inventory_with_market", (inventory, market)))
            refreshed_actions = bidder.prepare_exit_actions(client, config)
            self.queue.put(("exit_actions", refreshed_actions))
            self.queue.put(("status", "出货完成"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
            self.queue.put(("status", "出货失败"))
        finally:
            bidder.log = old_log
            self.queue.put(("busy", False))
    def build_limit_exit_actions(
        self,
        items: list[bidder.InventoryItem],
        snapshots: dict[str, bidder.MarketSnapshot],
        target_price: Decimal,
        action_mode: str,
        config: dict,
    ) -> list[bidder.ExitAction]:
        actions: list[bidder.ExitAction] = []
        rule = {
            "enabled": True,
            "expected_price": float(target_price),
            "action_mode": action_mode,
            "max_quantity": len(items),
        }
        for item in items:
            if item.status_text != "可交易":
                self.queue.put(("log", f"跳过未可交易库存：{item.name} | {item.display_cooldown}"))
                continue
            snapshot = snapshots.get(self.inventory_key(item))
            if snapshot is None:
                snapshot = bidder.MarketSnapshot(
                    template_id=item.template_id,
                    name=item.name,
                    market_hash_name=item.market_hash_name,
                    lowest_sell_price=item.reference_price,
                    highest_purchase_price=None,
                    on_sale_count=None,
                    raw={},
                )

            highest_bid = snapshot.highest_purchase_price
            if action_mode in {"bid_only", "bid_or_sale"} and highest_bid is not None and highest_bid >= target_price:
                price = exit_manager.action_price(highest_bid, config)
                actions.append(
                    bidder.ExitAction(
                        "drop_to_bid",
                        item,
                        target_price,
                        price,
                        snapshot,
                        rule,
                        f"限价 {target_price}，最高求购 {highest_bid} 已达标，按求购价出货",
                    )
                )
                continue

            if action_mode == "bid_only":
                self.queue.put(("log", f"跳过丢求购：{item.name} | 最高求购 {highest_bid or '-'} 低于限价 {target_price}"))
                continue

            price = exit_manager.action_price(target_price, config)
            actions.append(
                bidder.ExitAction(
                    "list_sale",
                    item,
                    target_price,
                    price,
                    snapshot,
                    rule,
                    f"限价 {target_price}，当前最高求购 {highest_bid or '-'} 未达标，按限价上架",
                )
            )
        return actions
    def run_execute_limit_inventory(
        self,
        config: dict,
        token: str,
        device_token: str,
        items: list[bidder.InventoryItem],
        target_price: Decimal,
        action_mode: str,
    ) -> None:
        old_log = bidder.log

        def ui_log(message: str) -> None:
            self.queue.put(("log", message))

        bidder.log = ui_log
        try:
            client = self.make_client(config, token, device_token)
            market = self.fetch_inventory_market_snapshots(client, items, config)
            actions = self.build_limit_exit_actions(items, market, target_price, action_mode, config)
            if not actions:
                raise bidder.YouPinError("没有满足限价条件的可执行出货动作。")
            submitted = bidder.execute_exit_actions(client, actions, config, live=True)
            self.queue.put(("log", f"限价出货已提交 {len(submitted)} 个动作"))
            inventory = market_data.fetch_inventory_items(client, config)
            refreshed_market = self.fetch_inventory_market_snapshots(client, inventory, config)
            self.queue.put(("inventory_with_market", (inventory, refreshed_market)))
            refreshed_actions = bidder.prepare_exit_actions(client, config)
            self.queue.put(("exit_actions", refreshed_actions))
            self.queue.put(("status", "限价出货完成"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
            self.queue.put(("status", "限价出货失败"))
        finally:
            bidder.log = old_log
            self.queue.put(("busy", False))
    def run_price_alert_check_once(self, config: dict, token: str, device_token: str) -> dict:
        client = self.make_client(config, token, device_token)
        status = price_alert.check_price_alerts(client, config)
        if status.get("sent"):
            self.queue.put(("log", f"价格预警已发送：{status.get('email_to')} | 新触发 {status.get('new_triggered_count', 0)} 条"))
            self.queue.put(("price_alert_status", f"价格预警已发送 | 新触发 {status.get('new_triggered_count', 0)} 条"))
        else:
            reason = status.get("reason", "checked")
            self.queue.put(("price_alert_status", f"价格预警已检查 | 触发 {status.get('triggered_count', 0)} | {reason}"))
        return status
    def run_check_price_alert_now(self, config: dict, token: str, device_token: str) -> None:
        try:
            self.run_price_alert_check_once(config, token, device_token)
            self.queue.put(("status", "价格预警检查完成"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
            self.queue.put(("status", "价格预警检查失败"))
        finally:
            self.queue.put(("busy", False))
