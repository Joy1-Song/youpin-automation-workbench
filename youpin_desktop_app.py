from __future__ import annotations

import copy
import os
import queue
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import youpin_major_bidder as bidder
import youpin_config as config_engine
import youpin_exit_manager as exit_manager
import youpin_execution_bridge as execution_bridge
import youpin_daily_report as daily_report
import youpin_market_data as market_data
import youpin_price_alert as price_alert
import youpin_desktop_config as desktop_config
from youpin_desktop_command_mixin import DesktopCommandMixin
from youpin_desktop_inventory_mixin import DesktopInventoryMixin
from youpin_desktop_constants import (
    BID_MODE_TO_VALUE,
    BID_VALUE_TO_MODE,
    CREATE_CHANNEL_TO_VALUE,
    CREATE_VALUE_TO_CHANNEL,
    EXIT_MODE_TO_VALUE,
    EXIT_VALUE_TO_MODE,
    SELECTION_MODE_TO_VALUE,
    SELECTION_VALUE_TO_MODE,
    build_strategy_profiles,
    register_strategy_profile_modes,
)
from youpin_desktop_style import THEME, configure_desktop_style
from youpin_desktop_utils import load_env_file, money_text


class YouPinDesktopApp(DesktopInventoryMixin, DesktopCommandMixin):
    def __init__(self, root: tk.Tk):
        load_env_file()
        self.root = root
        self.root.title("悠悠有品 2026 全息战队贴纸求购助手")
        self.root.geometry("1500x900")
        self.root.minsize(1280, 760)

        self.queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker_running = False
        self.auto_stop = threading.Event()
        self.auto_thread: threading.Thread | None = None
        self.current_actions_by_iid: dict[str, bidder.PlanAction] = {}
        self.current_orders_by_iid: dict[str, bidder.ExistingPurchaseOrder] = {}
        self.current_inventory_by_iid: dict[str, bidder.InventoryItem] = {}
        self.current_inventory_market_by_key: dict[str, bidder.MarketSnapshot] = {}
        self.current_exit_actions_by_iid: dict[str, bidder.ExitAction] = {}
        self.current_commands_by_iid: dict[str, dict] = {}
        self.current_workbench_commands_by_iid: dict[str, dict] = {}
        default_config = Path("config.local.json")
        initial_config = desktop_config.load_initial_config(default_config)
        initial_bid = initial_config.get("bid_strategy", {})
        initial_quantity = initial_config.get("quantity_strategy", {})
        initial_selection = initial_config.get("selection_strategy", {})
        initial_exit = initial_config.get("exit_strategy", {})
        initial_execution = initial_config.get("execution", {})
        initial_risk = initial_config.get("purchase_risk", {})
        initial_daily_report = initial_config.get("daily_report", {}) if isinstance(initial_config.get("daily_report", {}), dict) else {}
        initial_price_alert = initial_config.get("price_alert", {}) if isinstance(initial_config.get("price_alert", {}), dict) else {}
        self.strategy_profiles = build_strategy_profiles(initial_config)
        register_strategy_profile_modes(self.strategy_profiles)

        self.config_path = tk.StringVar(value=str(default_config))
        self.token = tk.StringVar(value=os.getenv("YOUPIN_TOKEN", ""))
        self.device_token = tk.StringVar(value=os.getenv("YOUPIN_DEVICE_TOKEN", ""))
        self.budget = tk.StringVar(value=str(initial_quantity.get("budget", initial_bid.get("max_daily_spend", 1000))))
        self.refreshes_per_day = tk.StringVar(value=str(initial_config.get("refreshes_per_day", 8)))
        self.max_quantity_per_item = tk.StringVar(value=str(initial_quantity.get("max_quantity_per_item", 5)))
        self.active_order_exposure_multiplier = tk.StringVar(value=str(initial_risk.get("active_order_exposure_multiplier", 2)))
        self.target_rank = tk.StringVar(value=str(initial_bid.get("target_rank", 2)))
        self.modify_interval_seconds = tk.StringVar(value=str(initial_bid.get("modify_interval_seconds", 60)))
        self.dry_run = tk.BooleanVar(value=bool(initial_config.get("dry_run", True)))
        self.live_enabled = tk.BooleanVar(value=False)
        self.allow_create_orders = tk.BooleanVar(value=bool(initial_bid.get("allow_create_orders", False)))
        initial_cleanup = initial_config.get("cleanup_strategy", {}) if isinstance(initial_config.get("cleanup_strategy", {}), dict) else {}
        self.cleanup_auto_execute = tk.BooleanVar(value=bool(initial_cleanup.get("auto_execute", True)))
        initial_guard = initial_config.get("cs_update_guard", {}) if isinstance(initial_config.get("cs_update_guard", {}), dict) else {}
        self.cs_update_guard_enabled = tk.BooleanVar(value=bool(initial_guard.get("enabled", True)))
        self.create_order_channel = tk.StringVar(
            value=CREATE_VALUE_TO_CHANNEL.get(str(initial_execution.get("create_order_channel", "api")), "API direct")
        )
        self.strategy_profile = tk.StringVar(value=desktop_config.detect_strategy_profile(initial_config, self.strategy_profiles))
        self.strategy_profile_note = tk.StringVar(value="")
        self.quantity_mode_value = tk.StringVar(value=str(initial_quantity.get("mode", "equal_budget")))
        self.selection_mode = tk.StringVar(
            value=SELECTION_VALUE_TO_MODE.get(str(initial_selection.get("mode", "cheapest")), str(initial_selection.get("mode", "cheapest")))
        )
        self.bid_mode = tk.StringVar(value=BID_VALUE_TO_MODE.get(str(initial_bid.get("mode", "rank_chase")), "追求购顺位"))
        self.exit_auto_execute = tk.BooleanVar(value=bool(initial_exit.get("auto_execute", False)))
        self.exit_target_price = tk.StringVar(value="")
        self.exit_rule_quantity = tk.StringVar(value="1")
        self.daily_report_enabled = tk.BooleanVar(value=bool(initial_daily_report.get("enabled", False)))
        self.daily_report_email = tk.StringVar(value=str(initial_daily_report.get("email_to", "")))
        self.daily_report_time = tk.StringVar(
            value=f"{int(initial_daily_report.get('scheduled_hour', 0)):02d}:{int(initial_daily_report.get('scheduled_minute', 0)):02d}"
        )
        self.daily_report_status = tk.StringVar(value="日报：未启用")
        self.price_alert_enabled = tk.BooleanVar(value=bool(initial_price_alert.get("enabled", False)))
        self.price_alert_bid_price = tk.StringVar(value="")
        self.price_alert_sale_price = tk.StringVar(value="")
        self.price_alert_status = tk.StringVar(value="价格预警：未启用")
        self.exit_action_mode = tk.StringVar(value=EXIT_VALUE_TO_MODE.get(str(initial_exit.get("default_action_mode", "bid_or_sale")), "求购达标优先"))
        self.selected_inventory_text = tk.StringVar(value="未选择库存")
        self.selected_market_text = tk.StringVar(value="卖一 -    最高求购 -    成本 -    价差 -")

        self.status = tk.StringVar(value="就绪")
        self.mode_text = tk.StringVar(value="预览模式")
        self.summary = tk.StringVar(value="计划总金额：0.00    本轮新增风险：0.00    品类数：0")
        self.order_summary = tk.StringVar(value="当前有效求购单：0")
        self.inventory_summary = tk.StringVar(value="库存资产：0    CD中：0    可交易：0    购入成本：0.00")
        self.exit_summary = tk.StringVar(value="自动卖出规则：0    达标动作：0")
        self.risk_summary = tk.StringVar(value="预算 1000 | 单品 5 | 刷新 8 次/天")

        self.configure_style()
        self.command_summary = tk.StringVar(value="APP commands: 0 pending")
        self.emulator_status_text = tk.StringVar(value="模拟器：未检测")
        self.build_ui()
        self.bind_summary_updates()
        self.update_strategy_profile_note()
        self.root.after(300, self.refresh_app_commands)
        self.root.after(200, self.drain_queue)

    def configure_style(self) -> None:
        configure_desktop_style(self.root)

    def update_strategy_profile_note(self) -> None:
        profile = self.strategy_profiles.get(self.strategy_profile.get(), {})
        self.strategy_profile_note.set(str(profile.get("note", "")))

    def apply_strategy_profile(self) -> None:
        profile = self.strategy_profiles.get(self.strategy_profile.get())
        if not profile:
            messagebox.showerror("策略错误", f"未知策略：{self.strategy_profile.get()}")
            return
        selection = str(profile.get("selection", "cheapest"))
        bid = str(profile.get("bid", "rank_chase"))
        quantity = str(profile.get("quantity", "equal_budget"))
        self.selection_mode.set(SELECTION_VALUE_TO_MODE.get(selection, selection))
        self.bid_mode.set(BID_VALUE_TO_MODE.get(bid, bid))
        self.quantity_mode_value.set(quantity)
        self.create_order_channel.set(CREATE_VALUE_TO_CHANNEL.get(str(profile.get("create_channel", "emulator_queue")), "APP command queue"))
        self.allow_create_orders.set(bool(profile.get("allow_create", True)))
        if "budget" in profile:
            self.budget.set(str(profile["budget"]))
        if "min_quantity_per_item" in profile:
            self.strategy_profile_min_quantity = int(profile["min_quantity_per_item"])
        if "max_quantity_per_item" in profile:
            self.max_quantity_per_item.set(str(profile["max_quantity_per_item"]))
        if "active_order_exposure_multiplier" in profile:
            self.active_order_exposure_multiplier.set(str(profile["active_order_exposure_multiplier"]))
        self.update_strategy_profile_note()
        self.update_mode_text()
        self.append_log(f"已应用策略：{self.strategy_profile.get()} | {self.strategy_profile_note.get()}")

    def bind_summary_updates(self) -> None:
        for variable in (
            self.budget,
            self.max_quantity_per_item,
            self.active_order_exposure_multiplier,
            self.refreshes_per_day,
            self.target_rank,
            self.selection_mode,
            self.bid_mode,
            self.create_order_channel,
            self.cleanup_auto_execute,
            self.cs_update_guard_enabled,
            self.daily_report_enabled,
            self.daily_report_email,
            self.daily_report_time,
            self.price_alert_enabled,
        ):
            variable.trace_add("write", lambda *_: self.update_mode_text())
        self.strategy_profile.trace_add("write", lambda *_: self.update_strategy_profile_note())

    def build_ui(self) -> None:
        self.build_header()

        body = ttk.Frame(self.root, padding=(12, 6, 12, 12))
        body.pack(fill=tk.BOTH, expand=True)

        body.columnconfigure(0, weight=0, minsize=380)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body, style="Panel.TFrame")
        left.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 12))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky=tk.NSEW)
        right.rowconfigure(0, weight=0)
        right.rowconfigure(1, weight=1)
        right.rowconfigure(2, weight=0)
        right.columnconfigure(0, weight=1)

        self.build_scrollable_control_panel(left)
        self.build_market_strip(right)
        self.build_tables(right)
        self.build_log(right)

    def build_header(self) -> None:
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(16, 14, 16, 8))
        header.pack(fill=tk.X)
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text="悠悠有品交易控制台", style="Header.TLabel").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(
            header,
            text="Cologne 2026 贴纸求购、库存、出货止盈一体化面板",
            style="HeaderSub.TLabel",
        ).grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        ttk.Label(header, textvariable=self.status, style="Header.TLabel", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=1, sticky=tk.E)
        ttk.Label(header, textvariable=self.risk_summary, style="HeaderSub.TLabel").grid(row=1, column=1, sticky=tk.E, pady=(4, 0))

    def build_market_strip(self, parent: ttk.Frame) -> None:
        strip = ttk.Frame(parent)
        strip.grid(row=0, column=0, sticky=tk.EW, pady=(0, 10))
        for col in range(4):
            strip.columnconfigure(col, weight=1)
        self.add_metric(strip, "求购计划", self.summary, 0)
        self.add_metric(strip, "当前挂单", self.order_summary, 1)
        self.add_metric(strip, "库存仓库", self.inventory_summary, 2)
        self.add_metric(strip, "自动卖出", self.exit_summary, 3)

    def add_metric(self, parent: ttk.Frame, title: str, variable: tk.StringVar, column: int) -> None:
        frame = ttk.Frame(parent, style="Metric.TFrame", padding=(12, 10))
        frame.grid(row=0, column=column, sticky=tk.NSEW, padx=(0 if column == 0 else 8, 0))
        ttk.Label(frame, text=title, style="MetricTitle.TLabel").pack(anchor=tk.W)
        ttk.Label(frame, textvariable=variable, style="MetricValue.TLabel", wraplength=250).pack(anchor=tk.W, pady=(5, 0))

    def build_scrollable_control_panel(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

        canvas = tk.Canvas(parent, background=THEME["surface"], highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky=tk.NSEW)
        scrollbar.grid(row=0, column=1, sticky=tk.NS)

        content = ttk.Frame(canvas, style="Sidebar.TFrame", padding=12)
        content_window = canvas.create_window((0, 0), window=content, anchor=tk.NW)

        def resize_scroll_region(_event: tk.Event) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def resize_content(event: tk.Event) -> None:
            canvas.itemconfigure(content_window, width=event.width)

        def scroll_sidebar(event: tk.Event) -> str:
            if event.delta:
                canvas.yview_scroll(-int(event.delta / 120), "units")
            return "break"

        content.bind("<Configure>", resize_scroll_region)
        canvas.bind("<Configure>", resize_content)

        self.build_control_panel(content)

        def bind_mousewheel(widget: tk.Widget) -> None:
            widget.bind("<MouseWheel>", scroll_sidebar, add="+")
            for child in widget.winfo_children():
                bind_mousewheel(child)

        bind_mousewheel(content)
        canvas.bind("<MouseWheel>", scroll_sidebar, add="+")

    def build_control_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        self.add_section_title(parent, "运行模式", 0)
        mode = ttk.Frame(parent, style="Section.TFrame", padding=10)
        mode.grid(row=1, column=0, sticky=tk.EW, pady=(0, 12))
        mode.columnconfigure(0, weight=1)
        ttk.Checkbutton(mode, text="dry_run 只预览", variable=self.dry_run, command=self.update_mode_text).grid(row=0, column=0, sticky=tk.W, pady=2)
        ttk.Checkbutton(mode, text="允许实盘按钮", variable=self.live_enabled, command=self.update_mode_text).grid(row=1, column=0, sticky=tk.W, pady=2)
        ttk.Checkbutton(mode, text="允许新建求购单", variable=self.allow_create_orders).grid(row=2, column=0, sticky=tk.W, pady=2)
        ttk.Checkbutton(mode, text="Auto cleanup non-strategy bids", variable=self.cleanup_auto_execute, command=self.update_mode_text).grid(row=3, column=0, sticky=tk.W, pady=2)
        ttk.Checkbutton(mode, text="CS update kill switch", variable=self.cs_update_guard_enabled, command=self.update_mode_text).grid(row=4, column=0, sticky=tk.W, pady=2)
        ttk.Label(mode, text="Create channel", style="SectionMuted.TLabel").grid(row=5, column=0, sticky=tk.W, pady=(8, 2))
        ttk.Combobox(
            mode,
            textvariable=self.create_order_channel,
            values=list(CREATE_CHANNEL_TO_VALUE.keys()),
            state="readonly",
            width=20,
        ).grid(row=6, column=0, sticky=tk.EW, pady=(0, 4))
        ttk.Label(mode, textvariable=self.mode_text, style="Status.TLabel").grid(row=7, column=0, sticky=tk.W, pady=(6, 0))

        self.add_section_title(parent, "策略参数", 4)
        params = ttk.Frame(parent, style="Section.TFrame", padding=10)
        params.grid(row=5, column=0, sticky=tk.EW, pady=(0, 12))
        params.columnconfigure(1, weight=1)
        ttk.Label(params, text="选品策略", style="SectionMuted.TLabel").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Combobox(
            params,
            textvariable=self.selection_mode,
            values=list(SELECTION_MODE_TO_VALUE.keys()),
            state="readonly",
            width=18,
        ).grid(row=0, column=1, sticky=tk.EW, padx=(8, 0), pady=4)
        ttk.Label(params, text="定价策略", style="SectionMuted.TLabel").grid(row=1, column=0, sticky=tk.W, pady=4)
        ttk.Combobox(
            params,
            textvariable=self.bid_mode,
            values=list(BID_MODE_TO_VALUE.keys()),
            state="readonly",
            width=18,
        ).grid(row=1, column=1, sticky=tk.EW, padx=(8, 0), pady=4)
        self.add_labeled_entry(params, "总预算", self.budget, 2)
        self.add_labeled_entry(params, "每天刷新次数", self.refreshes_per_day, 3)
        self.add_labeled_entry(params, "单品数量上限", self.max_quantity_per_item, 4)
        self.add_labeled_entry(params, "挂单风险倍率", self.active_order_exposure_multiplier, 5)
        self.add_labeled_entry(params, "目标顺位", self.target_rank, 6)
        self.add_labeled_entry(params, "修改间隔秒", self.modify_interval_seconds, 7)

        self.add_section_title(parent, "配置与登录", 6)
        auth = ttk.Frame(parent, style="Section.TFrame", padding=10)
        auth.grid(row=7, column=0, sticky=tk.EW, pady=(0, 12))
        auth.columnconfigure(1, weight=1)
        self.add_labeled_entry(auth, "配置文件", self.config_path, 0)
        ttk.Button(auth, text="选择", command=self.choose_config).grid(row=0, column=2, padx=(6, 0), sticky=tk.EW)
        self.add_labeled_entry(auth, "Token", self.token, 1, show="*")
        self.add_labeled_entry(auth, "DeviceToken", self.device_token, 2, show="*")
        ttk.Button(auth, text="重读 .env", command=self.reload_env).grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=(8, 0))
        ttk.Button(auth, text="保存配置", command=self.save_runtime_config).grid(row=4, column=0, columnspan=3, sticky=tk.EW, pady=(6, 0))

        self.add_section_title(parent, "操作", 2)
        actions = ttk.Frame(parent, style="Section.TFrame", padding=10)
        actions.grid(row=3, column=0, sticky=tk.EW, pady=(0, 12))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self.preview_button = ttk.Button(actions, text="刷新预览计划", command=self.preview_once, style="Primary.TButton")
        self.preview_button.grid(row=0, column=0, sticky=tk.EW, padx=(0, 6), pady=(0, 6))
        self.orders_button = ttk.Button(actions, text="刷新当前求购单", command=self.refresh_orders)
        self.orders_button.grid(row=0, column=1, sticky=tk.EW, pady=(0, 6))
        self.inventory_button = ttk.Button(actions, text="刷新库存仓库", command=self.refresh_inventory)
        self.inventory_button.grid(row=1, column=0, sticky=tk.EW, padx=(0, 6), pady=(0, 6))
        ttk.Button(actions, text="读取后台状态", command=self.load_runner_status).grid(row=1, column=1, sticky=tk.EW, pady=(0, 6))
        self.live_once_button = ttk.Button(actions, text="实盘执行一次", command=self.live_once)
        self.live_once_button.grid(row=2, column=0, sticky=tk.EW, padx=(0, 6), pady=(0, 6))
        self.auto_button = ttk.Button(actions, text="开始自动模式", command=self.start_auto)
        self.auto_button.grid(row=2, column=1, sticky=tk.EW, pady=(0, 6))
        self.stop_button = ttk.Button(actions, text="停止自动模式", command=self.stop_auto)
        self.stop_button.grid(row=3, column=0, sticky=tk.EW, padx=(0, 6))
        self.delete_button = ttk.Button(actions, text="撤销选中求购单", command=self.delete_selected_orders, style="Danger.TButton")
        self.delete_button.grid(row=3, column=1, sticky=tk.EW)
        self.queue_create_button = ttk.Button(actions, text="Generate APP commands", command=self.queue_create_commands)
        self.queue_create_button.grid(row=4, column=0, sticky=tk.EW, padx=(0, 6), pady=(6, 0))
        self.refresh_commands_button = ttk.Button(actions, text="Refresh APP commands", command=self.refresh_app_commands)
        self.refresh_commands_button.grid(row=4, column=1, sticky=tk.EW, pady=(6, 0))

        self.add_section_title(parent, "日报与成本", 8)
        report = ttk.Frame(parent, style="Section.TFrame", padding=10)
        report.grid(row=9, column=0, sticky=tk.EW, pady=(0, 12))
        report.columnconfigure(1, weight=1)
        ttk.Checkbutton(report, text="每日邮件日报", variable=self.daily_report_enabled, command=self.update_mode_text).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=2)
        self.add_labeled_entry(report, "收件邮箱", self.daily_report_email, 1)
        self.add_labeled_entry(report, "发送时间", self.daily_report_time, 2)
        self.one_click_buy_price_fill_button = ttk.Button(report, text="一键填入购入价", command=self.one_click_buy_price_fill, style="Primary.TButton")
        self.one_click_buy_price_fill_button.grid(row=3, column=0, sticky=tk.EW, padx=(0, 6), pady=(8, 0))
        self.send_daily_report_button = ttk.Button(report, text="立即发送日报", command=self.send_daily_report_now)
        self.send_daily_report_button.grid(row=3, column=1, sticky=tk.EW, pady=(8, 0))
        ttk.Label(report, textvariable=self.daily_report_status, style="SectionMuted.TLabel", wraplength=310).grid(row=4, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))
        ttk.Checkbutton(report, text="库存价格预警", variable=self.price_alert_enabled, command=self.update_mode_text).grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        ttk.Label(report, textvariable=self.price_alert_status, style="SectionMuted.TLabel", wraplength=310).grid(row=6, column=0, columnspan=2, sticky=tk.EW, pady=(6, 0))

        self.add_section_title(parent, "摘要", 10)
        summary = ttk.Frame(parent, style="Section.TFrame", padding=10)
        summary.grid(row=11, column=0, sticky=tk.EW, pady=(0, 12))
        ttk.Label(summary, textvariable=self.summary, style="SectionMuted.TLabel", wraplength=310).pack(anchor=tk.W)
        ttk.Label(summary, textvariable=self.order_summary, style="SectionMuted.TLabel", wraplength=310).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(summary, textvariable=self.inventory_summary, style="SectionMuted.TLabel", wraplength=310).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(summary, textvariable=self.exit_summary, style="SectionMuted.TLabel", wraplength=310).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(summary, textvariable=self.price_alert_status, style="SectionMuted.TLabel", wraplength=310).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(summary, textvariable=self.command_summary, style="SectionMuted.TLabel", wraplength=310).pack(anchor=tk.W, pady=(4, 0))
        self.update_mode_text()

    def build_tables(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent)
        self.notebook = notebook
        notebook.grid(row=1, column=0, sticky=tk.NSEW)

        workbench_tab = ttk.Frame(notebook, padding=8)
        plan_tab = ttk.Frame(notebook, padding=8)
        orders_tab = ttk.Frame(notebook, padding=8)
        inventory_tab = ttk.Frame(notebook, padding=8)
        commands_tab = ttk.Frame(notebook, padding=8)
        notebook.add(workbench_tab, text="交易工作台")
        notebook.add(plan_tab, text="计划挂单")
        notebook.add(orders_tab, text="当前已有求购")
        notebook.add(inventory_tab, text="库存仓库")

        self.build_workbench_tab(workbench_tab)

        self.plan_tree = self.make_tree(
            plan_tab,
            columns=("idx", "action", "name", "sell", "bid", "qty", "spend", "rank", "book_top", "selection_reason", "order_no"),
            headings={
                "idx": "#",
                "action": "动作",
                "name": "贴纸",
                "sell": "卖一",
                "bid": "目标求购",
                "qty": "数量",
                "spend": "金额",
                "rank": "当前顺位",
                "book_top": "盘口最高",
                "selection_reason": "选品理由",
                "order_no": "已有单号",
            },
            widths={"idx": 42, "action": 82, "name": 300, "sell": 80, "bid": 90, "qty": 60, "spend": 80, "rank": 80, "book_top": 85, "selection_reason": 260, "order_no": 150},
        )

        self.orders_tree = self.make_tree(
            orders_tab,
            columns=("idx", "name", "price", "qty", "rank", "book_top", "spend", "order_no"),
            headings={
                "idx": "#",
                "name": "贴纸",
                "price": "当前求购",
                "qty": "数量",
                "rank": "顺位",
                "book_top": "盘口最高",
                "spend": "金额",
                "order_no": "求购单号",
            },
            widths={"idx": 42, "name": 380, "price": 90, "qty": 65, "rank": 65, "book_top": 90, "spend": 85, "order_no": 170},
        )

        notebook.add(commands_tab, text="APP Commands")

        self.build_inventory_tab(inventory_tab)
        self.build_commands_tab(commands_tab)

    def build_workbench_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(1, weight=1)

        strategy = ttk.Frame(parent, style="Panel.TFrame", padding=10)
        strategy.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 8), pady=(0, 8))
        strategy.columnconfigure(1, weight=1)
        ttk.Label(strategy, text="策略与求购执行", style="Title.TLabel").grid(row=0, column=0, columnspan=4, sticky=tk.W)
        ttk.Label(strategy, text="交易策略", style="Muted.TLabel").grid(row=1, column=0, sticky=tk.W, pady=(10, 4))
        ttk.Combobox(
            strategy,
            textvariable=self.strategy_profile,
            values=list(self.strategy_profiles.keys()),
            state="readonly",
            width=22,
        ).grid(row=1, column=1, columnspan=2, sticky=tk.EW, padx=(8, 0), pady=(10, 4))
        ttk.Button(strategy, text="应用", command=self.apply_strategy_profile, style="Primary.TButton").grid(row=1, column=3, sticky=tk.EW, padx=(8, 0), pady=(10, 4))
        ttk.Label(strategy, textvariable=self.strategy_profile_note, style="Muted.TLabel", wraplength=480).grid(row=2, column=0, columnspan=4, sticky=tk.EW, pady=(0, 8))

        ttk.Label(strategy, text="选品", style="Muted.TLabel").grid(row=3, column=0, sticky=tk.W, pady=4)
        ttk.Combobox(strategy, textvariable=self.selection_mode, values=list(SELECTION_MODE_TO_VALUE.keys()), state="readonly").grid(row=3, column=1, sticky=tk.EW, padx=(8, 0), pady=4)
        ttk.Label(strategy, text="定价", style="Muted.TLabel").grid(row=3, column=2, sticky=tk.W, padx=(12, 0), pady=4)
        ttk.Combobox(strategy, textvariable=self.bid_mode, values=list(BID_MODE_TO_VALUE.keys()), state="readonly").grid(row=3, column=3, sticky=tk.EW, padx=(8, 0), pady=4)
        ttk.Label(strategy, text="预算", style="Muted.TLabel").grid(row=4, column=0, sticky=tk.W, pady=4)
        ttk.Entry(strategy, textvariable=self.budget, width=10).grid(row=4, column=1, sticky=tk.W, padx=(8, 0), pady=4)
        ttk.Label(strategy, text="单品上限", style="Muted.TLabel").grid(row=4, column=2, sticky=tk.W, padx=(12, 0), pady=4)
        ttk.Entry(strategy, textvariable=self.max_quantity_per_item, width=8).grid(row=4, column=3, sticky=tk.W, padx=(8, 0), pady=4)
        ttk.Label(strategy, text="目标顺位", style="Muted.TLabel").grid(row=5, column=0, sticky=tk.W, pady=4)
        ttk.Entry(strategy, textvariable=self.target_rank, width=10).grid(row=5, column=1, sticky=tk.W, padx=(8, 0), pady=4)
        ttk.Label(strategy, text="刷新/天", style="Muted.TLabel").grid(row=5, column=2, sticky=tk.W, padx=(12, 0), pady=4)
        ttk.Entry(strategy, textvariable=self.refreshes_per_day, width=8).grid(row=5, column=3, sticky=tk.W, padx=(8, 0), pady=4)
        ttk.Checkbutton(strategy, text="dry_run", variable=self.dry_run, command=self.update_mode_text).grid(row=6, column=0, sticky=tk.W, pady=(8, 4))
        ttk.Checkbutton(strategy, text="允许实盘", variable=self.live_enabled, command=self.update_mode_text).grid(row=6, column=1, sticky=tk.W, pady=(8, 4))
        ttk.Checkbutton(strategy, text="允许新建求购", variable=self.allow_create_orders).grid(row=6, column=2, sticky=tk.W, pady=(8, 4))
        ttk.Combobox(strategy, textvariable=self.create_order_channel, values=list(CREATE_CHANNEL_TO_VALUE.keys()), state="readonly", width=18).grid(row=6, column=3, sticky=tk.EW, padx=(8, 0), pady=(8, 4))

        ttk.Button(strategy, text="刷新计划", command=self.preview_once, style="Primary.TButton").grid(row=7, column=0, sticky=tk.EW, pady=(10, 0))
        ttk.Button(strategy, text="生成APP求购指令", command=self.queue_create_commands).grid(row=7, column=1, sticky=tk.EW, padx=(8, 0), pady=(10, 0))
        ttk.Button(strategy, text="实盘执行一次", command=self.live_once).grid(row=7, column=2, sticky=tk.EW, padx=(8, 0), pady=(10, 0))
        ttk.Button(strategy, text="自动模式", command=self.start_auto).grid(row=7, column=3, sticky=tk.EW, padx=(8, 0), pady=(10, 0))
        ttk.Label(strategy, textvariable=self.mode_text, style="Status.TLabel").grid(row=8, column=0, columnspan=4, sticky=tk.W, pady=(8, 0))

        exit_panel = ttk.Frame(parent, style="Panel.TFrame", padding=10)
        exit_panel.grid(row=0, column=1, sticky=tk.NSEW, pady=(0, 8))
        exit_panel.columnconfigure(1, weight=1)
        exit_panel.columnconfigure(3, weight=1)
        ttk.Label(exit_panel, text="库存限价出货", style="Title.TLabel").grid(row=0, column=0, columnspan=4, sticky=tk.W)
        ttk.Label(exit_panel, textvariable=self.selected_inventory_text, style="Muted.TLabel", wraplength=520).grid(row=1, column=0, columnspan=4, sticky=tk.EW, pady=(10, 4))
        ttk.Label(exit_panel, textvariable=self.selected_market_text, style="Muted.TLabel", wraplength=520).grid(row=2, column=0, columnspan=4, sticky=tk.EW, pady=(0, 8))
        ttk.Checkbutton(exit_panel, text="自动模式允许出货", variable=self.exit_auto_execute).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=4)
        ttk.Button(exit_panel, text="刷新库存", command=self.refresh_inventory).grid(row=3, column=2, sticky=tk.EW, padx=(8, 0), pady=4)
        ttk.Button(exit_panel, text="刷新出货状态", command=self.refresh_exit_plan).grid(row=3, column=3, sticky=tk.EW, padx=(8, 0), pady=4)
        ttk.Label(exit_panel, text="自动卖价", style="Muted.TLabel").grid(row=4, column=0, sticky=tk.W, pady=4)
        ttk.Entry(exit_panel, textvariable=self.exit_target_price, width=10).grid(row=4, column=1, sticky=tk.W, padx=(8, 0), pady=4)
        ttk.Label(exit_panel, text="数量", style="Muted.TLabel").grid(row=4, column=2, sticky=tk.W, padx=(12, 0), pady=4)
        ttk.Entry(exit_panel, textvariable=self.exit_rule_quantity, width=8).grid(row=4, column=3, sticky=tk.W, padx=(8, 0), pady=4)
        ttk.Label(exit_panel, text="出货模式", style="Muted.TLabel").grid(row=5, column=0, sticky=tk.W, pady=4)
        ttk.Combobox(exit_panel, textvariable=self.exit_action_mode, values=list(EXIT_MODE_TO_VALUE.keys()), state="readonly").grid(row=5, column=1, columnspan=3, sticky=tk.EW, padx=(8, 0), pady=4)
        ttk.Button(exit_panel, text="填卖一", command=lambda: self.fill_exit_price_from_selected("sell")).grid(row=6, column=0, sticky=tk.EW, pady=(10, 0))
        ttk.Button(exit_panel, text="填求购一", command=lambda: self.fill_exit_price_from_selected("bid")).grid(row=6, column=1, sticky=tk.EW, padx=(8, 0), pady=(10, 0))
        ttk.Button(exit_panel, text="成本+10%", command=lambda: self.fill_exit_price_from_selected("cost_plus")).grid(row=6, column=2, sticky=tk.EW, padx=(8, 0), pady=(10, 0))
        ttk.Button(exit_panel, text="保存卖价", command=self.save_exit_rule_for_selected_inventory, style="Primary.TButton").grid(row=6, column=3, sticky=tk.EW, padx=(8, 0), pady=(10, 0))
        ttk.Button(exit_panel, text="实盘限价出货", command=self.execute_limit_order_for_selected_inventory, style="Danger.TButton").grid(row=7, column=0, columnspan=2, sticky=tk.EW, pady=(10, 0))
        ttk.Button(exit_panel, text="删除卖价规则", command=self.delete_selected_exit_rules).grid(row=7, column=2, sticky=tk.EW, padx=(8, 0), pady=(10, 0))
        ttk.Button(exit_panel, text="停止自动", command=self.stop_auto).grid(row=7, column=3, sticky=tk.EW, padx=(8, 0), pady=(10, 0))
        ttk.Label(exit_panel, textvariable=self.exit_summary, style="Muted.TLabel").grid(row=8, column=0, columnspan=4, sticky=tk.W, pady=(8, 0))

        commands_panel = ttk.Frame(parent, style="Panel.TFrame", padding=10)
        commands_panel.grid(row=1, column=0, columnspan=2, sticky=tk.NSEW)
        commands_panel.rowconfigure(1, weight=1)
        commands_panel.columnconfigure(0, weight=1)
        ttk.Label(commands_panel, textvariable=self.command_summary, style="Title.TLabel").grid(row=0, column=0, sticky=tk.W)
        ttk.Button(commands_panel, text="刷新队列", command=self.refresh_app_commands).grid(row=0, column=1, sticky=tk.E)
        workbench_table = ttk.Frame(commands_panel)
        workbench_table.grid(row=1, column=0, columnspan=2, sticky=tk.NSEW, pady=(8, 0))
        self.workbench_commands_tree = self.make_tree(
            workbench_table,
            columns=("idx", "status", "name", "bid", "qty", "amount", "source", "created_at"),
            headings={
                "idx": "#",
                "status": "状态",
                "name": "物品",
                "bid": "求购价",
                "qty": "数量",
                "amount": "金额",
                "source": "来源",
                "created_at": "创建时间",
            },
            widths={"idx": 42, "status": 80, "name": 360, "bid": 80, "qty": 60, "amount": 85, "source": 95, "created_at": 150},
        )

    def build_inventory_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        controls = ttk.Frame(parent, style="Panel.TFrame", padding=10)
        controls.grid(row=0, column=0, sticky=tk.EW, pady=(0, 8))
        for column in range(8):
            controls.columnconfigure(column, weight=1 if column in {5, 7} else 0)

        ttk.Label(controls, text="库存自动卖出", style="Title.TLabel").grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        ttk.Label(controls, textvariable=self.selected_inventory_text, style="Muted.TLabel").grid(row=0, column=1, columnspan=5, sticky=tk.W)
        ttk.Checkbutton(controls, text="自动模式允许出货", variable=self.exit_auto_execute).grid(row=0, column=6, columnspan=2, sticky=tk.E)
        ttk.Label(controls, textvariable=self.selected_market_text, style="Muted.TLabel").grid(row=1, column=0, columnspan=8, sticky=tk.W, pady=(6, 8))

        ttk.Label(controls, text="自动卖价", style="Muted.TLabel").grid(row=2, column=0, sticky=tk.W, padx=(0, 6))
        ttk.Entry(controls, textvariable=self.exit_target_price, width=10).grid(row=2, column=1, sticky=tk.W, padx=(0, 12))
        ttk.Label(controls, text="数量", style="Muted.TLabel").grid(row=2, column=2, sticky=tk.W, padx=(0, 6))
        ttk.Entry(controls, textvariable=self.exit_rule_quantity, width=6).grid(row=2, column=3, sticky=tk.W, padx=(0, 12))
        ttk.Label(controls, text="模式", style="Muted.TLabel").grid(row=2, column=4, sticky=tk.W, padx=(0, 6))
        ttk.Combobox(
            controls,
            textvariable=self.exit_action_mode,
            values=list(EXIT_MODE_TO_VALUE.keys()),
            state="readonly",
            width=14,
        ).grid(row=2, column=5, sticky=tk.W, padx=(0, 12))

        ttk.Button(controls, text="填卖一", command=lambda: self.fill_exit_price_from_selected("sell")).grid(row=3, column=0, sticky=tk.EW, padx=(0, 6), pady=(8, 0))
        ttk.Button(controls, text="填求购一", command=lambda: self.fill_exit_price_from_selected("bid")).grid(row=3, column=1, sticky=tk.EW, padx=(0, 6), pady=(8, 0))
        ttk.Button(controls, text="成本+10%", command=lambda: self.fill_exit_price_from_selected("cost_plus")).grid(row=3, column=2, sticky=tk.EW, padx=(0, 6), pady=(8, 0))
        ttk.Button(controls, text="刷新选中盘口", command=self.refresh_selected_inventory_market).grid(row=3, column=3, columnspan=2, sticky=tk.EW, padx=(0, 6), pady=(8, 0))
        self.exit_save_rule_button = ttk.Button(controls, text="保存自动卖价", command=self.save_exit_rule_for_selected_inventory, style="Primary.TButton")
        self.exit_save_rule_button.grid(row=3, column=5, sticky=tk.EW, padx=(0, 6), pady=(8, 0))
        self.exit_limit_execute_button = ttk.Button(controls, text="实盘限价出货", command=self.execute_limit_order_for_selected_inventory, style="Danger.TButton")
        self.exit_limit_execute_button.grid(row=3, column=6, columnspan=2, sticky=tk.EW, pady=(8, 0))

        self.exit_refresh_button = ttk.Button(controls, text="刷新自动状态", command=self.refresh_exit_plan)
        self.exit_refresh_button.grid(row=4, column=0, columnspan=2, sticky=tk.EW, padx=(0, 6), pady=(8, 0))
        self.exit_delete_rule_button = ttk.Button(controls, text="删除自动卖价", command=self.delete_selected_exit_rules)
        self.exit_delete_rule_button.grid(row=4, column=2, columnspan=2, sticky=tk.EW, padx=(0, 6), pady=(8, 0))
        ttk.Label(controls, textvariable=self.exit_summary, style="Muted.TLabel").grid(row=4, column=4, columnspan=4, sticky=tk.E, pady=(8, 0))
        ttk.Checkbutton(controls, text="价格预警", variable=self.price_alert_enabled, command=self.update_mode_text).grid(row=5, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Label(controls, text="求购预警", style="Muted.TLabel").grid(row=5, column=1, sticky=tk.E, padx=(0, 6), pady=(8, 0))
        ttk.Entry(controls, textvariable=self.price_alert_bid_price, width=10).grid(row=5, column=2, sticky=tk.W, padx=(0, 6), pady=(8, 0))
        ttk.Label(controls, text="在售预警", style="Muted.TLabel").grid(row=5, column=3, sticky=tk.E, padx=(0, 6), pady=(8, 0))
        ttk.Entry(controls, textvariable=self.price_alert_sale_price, width=10).grid(row=5, column=4, sticky=tk.W, padx=(0, 6), pady=(8, 0))
        self.price_alert_save_button = ttk.Button(controls, text="保存预警", command=self.save_price_alert_for_selected_inventory, style="Primary.TButton")
        self.price_alert_save_button.grid(row=5, column=5, sticky=tk.EW, padx=(0, 6), pady=(8, 0))
        self.price_alert_delete_button = ttk.Button(controls, text="删除预警", command=self.delete_selected_price_alerts)
        self.price_alert_delete_button.grid(row=5, column=6, sticky=tk.EW, padx=(0, 6), pady=(8, 0))
        self.price_alert_check_button = ttk.Button(controls, text="检查预警", command=self.check_price_alert_now)
        self.price_alert_check_button.grid(row=5, column=7, sticky=tk.EW, pady=(8, 0))

        table_frame = ttk.Frame(parent)
        table_frame.grid(row=1, column=0, sticky=tk.NSEW)
        self.inventory_tree = self.make_tree(
            table_frame,
            columns=("idx", "name", "same_qty", "buy_price", "highest_bid", "lowest_sell", "spread", "auto_price", "bid_alert_price", "sale_alert_price", "exit_mode", "ref_price", "cooldown", "status", "asset_id"),
            headings={
                "idx": "#",
                "name": "库存物品",
                "same_qty": "同品库存",
                "buy_price": "购入价",
                "highest_bid": "最高求购",
                "lowest_sell": "卖一",
                "spread": "价差",
                "auto_price": "自动卖价",
                "bid_alert_price": "求购预警",
                "sale_alert_price": "在售预警",
                "exit_mode": "出货模式",
                "ref_price": "参考价",
                "cooldown": "CD/可交易时间",
                "status": "状态",
                "asset_id": "Steam Asset",
            },
            widths={"idx": 42, "name": 260, "same_qty": 72, "buy_price": 78, "highest_bid": 82, "lowest_sell": 82, "spread": 70, "auto_price": 82, "bid_alert_price": 78, "sale_alert_price": 78, "exit_mode": 100, "ref_price": 78, "cooldown": 130, "status": 78, "asset_id": 130},
        )
        self.inventory_tree.bind("<<TreeviewSelect>>", self.on_inventory_select, add="+")

    def build_commands_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(parent, style="Panel.TFrame", padding=10)
        toolbar.grid(row=0, column=0, sticky=tk.EW, pady=(0, 8))
        toolbar.columnconfigure(0, weight=1)
        ttk.Label(toolbar, textvariable=self.command_summary, style="Title.TLabel").grid(row=0, column=0, sticky=tk.W)
        self.refresh_commands_tab_button = ttk.Button(toolbar, text="刷新队列", command=self.refresh_app_commands)
        self.refresh_commands_tab_button.grid(row=0, column=1, sticky=tk.E, padx=(8, 0))
        self.emulator_check_button = ttk.Button(toolbar, text="启动/检测APP", command=self.emulator_check)
        self.emulator_check_button.grid(row=0, column=2, sticky=tk.E, padx=(8, 0))
        self.emulator_capture_button = ttk.Button(toolbar, text="截图/状态", command=self.emulator_capture)
        self.emulator_capture_button.grid(row=0, column=3, sticky=tk.E, padx=(8, 0))
        self.emulator_prepare_button = ttk.Button(toolbar, text="预填首条指令", command=self.emulator_prepare_first, style="Primary.TButton")
        self.emulator_prepare_button.grid(row=0, column=4, sticky=tk.E, padx=(8, 0))
        self.emulator_fill_tab_button = ttk.Button(toolbar, text="填首条求购单", command=self.emulator_fill_first)
        self.emulator_fill_tab_button.grid(row=0, column=5, sticky=tk.E, padx=(8, 0))
        self.emulator_buy_price_fill_button = ttk.Button(toolbar, text="一键补录购入价", command=self.one_click_buy_price_fill)
        self.emulator_buy_price_fill_button.grid(row=0, column=6, sticky=tk.E, padx=(8, 0))
        self.emulator_create_orders_button = ttk.Button(toolbar, text="自动执行求购队列", command=self.emulator_process_create_orders, style="Primary.TButton")
        self.emulator_create_orders_button.grid(row=0, column=7, sticky=tk.E, padx=(8, 0))
        ttk.Button(toolbar, text="打开截图", command=self.open_emulator_screenshot).grid(row=0, column=8, sticky=tk.E, padx=(8, 0))
        ttk.Label(toolbar, textvariable=self.emulator_status_text, style="Muted.TLabel", wraplength=980).grid(
            row=1,
            column=0,
            columnspan=9,
            sticky=tk.EW,
            pady=(8, 0),
        )

        table_frame = ttk.Frame(parent)
        table_frame.grid(row=1, column=0, sticky=tk.NSEW)
        self.commands_tree = self.make_tree(
            table_frame,
            columns=("idx", "status", "type", "name", "bid", "qty", "amount", "source", "created_at", "id"),
            headings={
                "idx": "#",
                "status": "Status",
                "type": "Type",
                "name": "Item",
                "bid": "Bid",
                "qty": "Qty",
                "amount": "Amount",
                "source": "Source",
                "created_at": "Created",
                "id": "Command ID",
            },
            widths={"idx": 42, "status": 85, "type": 140, "name": 340, "bid": 75, "qty": 60, "amount": 85, "source": 100, "created_at": 150, "id": 150},
        )

    def build_log(self, parent: ttk.Frame) -> None:
        log_frame = ttk.Frame(parent, style="Panel.TFrame", padding=8)
        log_frame.grid(row=2, column=0, sticky=tk.EW, pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        ttk.Label(log_frame, text="运行日志", style="Title.TLabel").grid(row=0, column=0, sticky=tk.W)
        ttk.Button(log_frame, text="清空日志", command=self.clear_log).grid(row=0, column=1, sticky=tk.E)
        self.log_text = tk.Text(log_frame, height=8, wrap=tk.WORD, relief=tk.FLAT, bg=THEME["surface"], fg=THEME["text"], insertbackground=THEME["text"], padx=10, pady=8)
        self.log_text.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))

    def make_tree(self, parent: ttk.Frame, columns: tuple[str, ...], headings: dict[str, str], widths: dict[str, int]) -> ttk.Treeview:
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="extended")
        yscroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        xscroll = ttk.Scrollbar(parent, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky=tk.NSEW)
        yscroll.grid(row=0, column=1, sticky=tk.NS)
        xscroll.grid(row=1, column=0, sticky=tk.EW)
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], minwidth=50, anchor=tk.W)
        self.configure_tree_tags(tree)
        return tree

    def configure_tree_tags(self, tree: ttk.Treeview) -> None:
        tree.tag_configure("positive", foreground=THEME["green"])
        tree.tag_configure("danger", foreground=THEME["red"])
        tree.tag_configure("warning", foreground=THEME["yellow"])
        tree.tag_configure("muted", foreground=THEME["muted"])
        tree.tag_configure("blue", foreground=THEME["blue"])
        tree.tag_configure("purple", foreground=THEME["purple"])

    def add_section_title(self, parent: ttk.Frame, text: str, row: int) -> None:
        ttk.Label(parent, text=text, style="Title.TLabel").grid(row=row, column=0, sticky=tk.W, pady=(0, 6))

    def add_labeled_entry(self, parent: ttk.Frame, label: str, variable: tk.StringVar, row: int, show: str | None = None) -> None:
        ttk.Label(parent, text=label, style="SectionMuted.TLabel").grid(row=row, column=0, sticky=tk.W, pady=4)
        ttk.Entry(parent, textvariable=variable, show=show or "", width=18).grid(row=row, column=1, sticky=tk.EW, padx=(8, 0), pady=4)

    def choose_config(self) -> None:
        path = filedialog.askopenfilename(
            title="选择配置文件",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialdir=str(Path.cwd()),
        )
        if path:
            self.config_path.set(path)

    def reload_env(self) -> None:
        load_env_file()
        self.token.set(os.getenv("YOUPIN_TOKEN", ""))
        self.device_token.set(os.getenv("YOUPIN_DEVICE_TOKEN", ""))
        self.append_log("已重新读取 .env")

    def parse_budget(self) -> Decimal:
        try:
            budget = Decimal(self.budget.get())
        except InvalidOperation as exc:
            raise bidder.YouPinError("总预算必须是数字。") from exc
        if budget <= 0:
            raise bidder.YouPinError("总预算必须大于 0。")
        return budget

    def parse_positive_decimal(self, value: tk.StringVar, field_name: str) -> Decimal:
        try:
            parsed = Decimal(value.get())
        except InvalidOperation as exc:
            raise bidder.YouPinError(f"{field_name} 必须是数字。") from exc
        if parsed <= 0:
            raise bidder.YouPinError(f"{field_name} 必须大于 0。")
        return parsed

    def parse_refreshes(self) -> int:
        return self.parse_positive_int(self.refreshes_per_day, "每天刷新次数")

    def parse_positive_int(self, value: tk.StringVar, field_name: str) -> int:
        try:
            parsed = int(value.get())
        except ValueError as exc:
            raise bidder.YouPinError(f"{field_name} 必须是整数。") from exc
        if parsed <= 0:
            raise bidder.YouPinError(f"{field_name} 必须大于 0。")
        return parsed

    def parse_nonnegative_int(self, value: tk.StringVar, field_name: str) -> int:
        try:
            parsed = int(value.get())
        except ValueError as exc:
            raise bidder.YouPinError(f"{field_name} 必须是整数。") from exc
        if parsed < 0:
            raise bidder.YouPinError(f"{field_name} 不能小于 0。")
        return parsed

    def parse_daily_report_time(self) -> tuple[int, int]:
        text = self.daily_report_time.get().strip() or "00:00"
        if ":" not in text:
            raise bidder.YouPinError("日报时间格式必须是 HH:MM，例如 00:00。")
        hour_text, minute_text = text.split(":", 1)
        try:
            hour = int(hour_text)
            minute = int(minute_text)
        except ValueError as exc:
            raise bidder.YouPinError("日报时间格式必须是 HH:MM，例如 00:00。") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise bidder.YouPinError("日报时间必须在 00:00 到 23:59 之间。")
        return hour, minute

    def runtime_config(self) -> dict:
        path = Path(self.config_path.get())
        load_path = path
        if not load_path.exists() and path.name == "config.local.json" and Path("config.example.json").exists():
            load_path = Path("config.example.json")
        config = bidder.load_config(load_path)
        profile = self.strategy_profiles.get(self.strategy_profile.get(), {})
        defaults = profile.get("config_defaults")
        if isinstance(defaults, dict):
            config = desktop_config.merge_defaults(config, defaults)
        budget = self.parse_budget()
        refreshes = self.parse_refreshes()
        max_qty = self.parse_positive_int(self.max_quantity_per_item, "单品数量上限")
        active_multiplier = self.parse_positive_decimal(self.active_order_exposure_multiplier, "挂单风险倍率")
        target_rank = self.parse_positive_int(self.target_rank, "目标顺位")
        modify_interval = self.parse_nonnegative_int(self.modify_interval_seconds, "修改间隔秒")
        max_qty_hard_limit = profile.get("max_quantity_hard_limit")
        if max_qty_hard_limit:
            max_qty = min(max_qty, int(max_qty_hard_limit))
            self.max_quantity_per_item.set(str(max_qty))
        report_time = self.parse_daily_report_time()
        settings = {
            "dry_run": bool(self.dry_run.get()),
            "refreshes_per_day": refreshes,
            "budget": budget,
            "max_quantity_per_item": max_qty,
            "active_order_exposure_multiplier": active_multiplier,
            "target_rank": target_rank,
            "modify_interval_seconds": modify_interval,
            "allow_create_orders": bool(self.allow_create_orders.get()),
            "selection_mode": self.selection_mode.get(),
            "bid_mode": self.bid_mode.get(),
            "quantity_mode": self.quantity_mode_value.get(),
            "daily_report_enabled": bool(self.daily_report_enabled.get()),
            "daily_report_email": self.daily_report_email.get().strip(),
            "daily_report_time": report_time,
            "price_alert_enabled": bool(self.price_alert_enabled.get()),
            "cleanup_auto_execute": bool(self.cleanup_auto_execute.get()),
            "cs_update_guard_enabled": bool(self.cs_update_guard_enabled.get()),
            "exit_auto_execute": bool(self.exit_auto_execute.get()),
            "create_order_channel": self.create_order_channel.get(),
        }
        return config_engine.apply_desktop_runtime_settings(
            config,
            settings,
            profile=profile,
            selection_mode_map=SELECTION_MODE_TO_VALUE,
            bid_mode_map=BID_MODE_TO_VALUE,
            create_channel_map=CREATE_CHANNEL_TO_VALUE,
        )

    def save_runtime_config(self) -> None:
        try:
            config = self.runtime_config()
            path = Path(self.config_path.get())
            path.write_text(bidder.json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self.append_log("当前设置已写入配置文件")
        messagebox.showinfo("已保存", "当前设置已写入配置文件。")

    def update_mode_text(self) -> None:
        if self.dry_run.get():
            self.mode_text.set("当前：只预览，不会真实改单")
        elif self.live_enabled.get():
            self.mode_text.set("当前：允许实盘操作")
        else:
            self.mode_text.set("当前：实盘按钮未解锁")
        self.risk_summary.set(
            f"预算 {self.budget.get()} | 单品 {self.max_quantity_per_item.get()} | "
            f"日内成交上限 {self.budget.get()} | 挂单倍率 {self.active_order_exposure_multiplier.get()}x | "
            f"刷新 {self.refreshes_per_day.get()} 次/天 | "
            f"{self.selection_mode.get()} | {self.bid_mode.get()}"
        )

        cleanup_text = "清理非策略单：开" if self.cleanup_auto_execute.get() else "清理非策略单：关"
        guard_text = "CS更新保护：开" if self.cs_update_guard_enabled.get() else "CS更新保护：关"
        self.risk_summary.set(self.risk_summary.get() + f" | {self.create_order_channel.get()} | {cleanup_text} | {guard_text}")
        report_text = "日报：开" if self.daily_report_enabled.get() else "日报：关"
        email_text = self.daily_report_email.get().strip() or "未填写邮箱"
        self.daily_report_status.set(f"{report_text} | {self.daily_report_time.get().strip() or '00:00'} | {email_text}")
        alert_text = "价格预警：开" if self.price_alert_enabled.get() else "价格预警：关"
        self.price_alert_status.set(f"{alert_text} | 每 45 分钟 | {email_text}")

    def set_busy(self, busy: bool) -> None:
        self.worker_running = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.preview_button.configure(state=state)
        self.orders_button.configure(state=state)
        self.inventory_button.configure(state=state)
        self.exit_refresh_button.configure(state=state)
        self.exit_limit_execute_button.configure(state=state)
        self.exit_save_rule_button.configure(state=state)
        self.exit_delete_rule_button.configure(state=state)
        self.live_once_button.configure(state=state)
        self.auto_button.configure(state=state if busy else tk.NORMAL)
        self.delete_button.configure(state=state)
        self.queue_create_button.configure(state=state)
        self.refresh_commands_button.configure(state=state)
        for name in (
            "refresh_commands_tab_button",
            "emulator_check_button",
            "emulator_capture_button",
            "emulator_prepare_button",
            "emulator_fill_button",
            "emulator_fill_tab_button",
            "emulator_buy_price_fill_button",
            "one_click_buy_price_fill_button",
            "send_daily_report_button",
            "price_alert_save_button",
            "price_alert_delete_button",
            "price_alert_check_button",
        ):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state=state)
        self.stop_button.configure(state=tk.NORMAL)

    def append_log(self, message: str) -> None:
        self.log_text.insert(tk.END, f"{bidder.now_text()}  {message}\n")
        self.log_text.see(tk.END)

    def clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)

    def load_runner_status(self) -> None:
        status_file = Path("state/auto_runner_status.json")
        log_file = Path("logs/auto_runner.log")
        if not status_file.exists():
            messagebox.showinfo("暂无状态", "还没有找到 state/auto_runner_status.json。后台任务跑过一轮后会生成。")
            return
        try:
            payload = bidder.json.loads(status_file.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))
            return

        def status_value(key: str) -> str:
            value = payload.get(key)
            if value in (None, ""):
                return "未记录（旧后台状态）"
            return str(value)

        next_run_text = str(payload.get("next_run_at", ""))
        stale_note = ""
        if next_run_text:
            try:
                next_run = datetime.fromisoformat(next_run_text)
                if next_run < datetime.now():
                    stale_note = "后台状态可能已过期：下次运行时间已经过去。"
            except ValueError:
                pass

        lines = [
            "说明：这里读取的是 Windows 后台计划任务状态，不是当前桌面自动模式日志。",
            f"后台状态 success={payload.get('success')}",
            f"更新时间：{payload.get('updated_at', '')}",
            f"下次运行：{payload.get('next_run_at', '')}",
            f"计划总额：{payload.get('planned_active_spend', '')}",
            f"新增风险：{payload.get('new_exposure', '')}",
            f"自动出货：{status_value('exit_auto_execute')}",
            f"自动卖出检查：{status_value('exit_action_count')}",
            f"达标出货：{status_value('exit_actionable_count')}",
            f"已提交出货：{status_value('exit_submitted_count')}",
            f"日报状态：{payload.get('daily_report', '')}",
            f"错误：{payload.get('last_error', '')}",
        ]
        if stale_note:
            lines.append(stale_note)
        for line in lines:
            self.append_log(line)

        if log_file.exists():
            try:
                recent = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-10:]
                for line in recent:
                    self.append_log("log: " + line)
            except Exception:
                pass

    def preview_once(self) -> None:
        self.start_worker(execute=False, live=False)

    def refresh_orders(self) -> None:
        if self.worker_running:
            return
        try:
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.set_busy(True)
        self.status.set("正在读取当前求购单")
        threading.Thread(
            target=self.run_refresh_orders,
            args=(config, self.token.get().strip(), self.device_token.get().strip()),
            daemon=True,
        ).start()

    def refresh_inventory(self) -> None:
        if self.worker_running:
            return
        try:
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.set_busy(True)
        self.status.set("正在读取库存仓库")
        threading.Thread(
            target=self.run_refresh_inventory,
            args=(config, self.token.get().strip(), self.device_token.get().strip()),
            daemon=True,
        ).start()















































    def live_once(self) -> None:
        if not self.live_enabled.get():
            messagebox.showwarning("未允许实盘", "请先勾选“允许实盘按钮”。")
            return
        if self.dry_run.get():
            messagebox.showwarning("仍是 dry_run", "请先取消“dry_run 只预览”。")
            return
        extra = "，并执行已开启的库存出货规则" if self.exit_auto_execute.get() else ""
        if not messagebox.askyesno("确认实盘", f"将按当前计划真实创建或更新悠悠有品求购单{extra}。确认继续？"):
            return
        self.start_worker(execute=True, live=True)

    def start_auto(self) -> None:
        if self.auto_thread and self.auto_thread.is_alive():
            messagebox.showinfo("已在运行", "自动模式已经在运行。")
            return
        if self.live_enabled.get() and not self.dry_run.get():
            extra = "，并自动执行达标出货规则" if self.exit_auto_execute.get() else ""
            ok = messagebox.askyesno("确认自动实盘", f"自动模式会按刷新频率真实更新求购单{extra}。确认继续？")
            if not ok:
                return
        try:
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        token = self.token.get().strip()
        device_token = self.device_token.get().strip()
        execute = self.live_enabled.get() and not self.dry_run.get()
        live = execute
        self.auto_stop.clear()
        self.set_busy(True)
        self.auto_thread = threading.Thread(target=self.auto_loop, args=(config, token, device_token, execute, live), daemon=True)
        self.auto_thread.start()
        self.status.set("自动模式运行中")

    def stop_auto(self) -> None:
        self.auto_stop.set()
        self.status.set("正在停止自动模式")

    def start_worker(self, execute: bool, live: bool) -> None:
        if self.worker_running:
            return
        try:
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        token = self.token.get().strip()
        device_token = self.device_token.get().strip()
        self.set_busy(True)
        self.status.set("运行中")
        threading.Thread(target=self.run_core, args=(config, token, device_token, execute, live), daemon=True).start()

    def make_client(self, config: dict, token: str, device_token: str) -> bidder.YouPinClient:
        return bidder.YouPinClient(
            config["api_base"],
            token=token,
            device_token=device_token,
            cookie=os.getenv("YOUPIN_COOKIE", ""),
            uk=os.getenv("YOUPIN_UK", ""),
            device_uk=os.getenv("YOUPIN_DEVICE_UK", device_token),
            device_id=os.getenv("YOUPIN_PC_UUID", ""),
        )

    def delete_selected_orders(self) -> None:
        if self.worker_running:
            return
        if not self.live_enabled.get() or self.dry_run.get():
            messagebox.showwarning("未允许实盘", "撤销求购单会真实生效，请先取消 dry_run 并勾选“允许实盘按钮”。")
            return
        selected = self.orders_tree.selection()
        if not selected:
            messagebox.showinfo("未选择", "请先在“当前已有求购”表中选择要撤销的求购单。")
            return

        orders: list[bidder.ExistingPurchaseOrder] = []
        for iid in selected:
            order = self.current_orders_by_iid.get(str(iid))
            if order:
                orders.append(order)
        if not orders:
            messagebox.showinfo("没有可撤销项", "选中的行里没有可撤销的已有求购单。")
            return

        lines = [f"{order.commodity_name}  {order.purchase_price} x {order.purchase_num}" for order in orders]
        message = "将真实撤销以下求购单：\n\n" + "\n".join(lines[:8])
        if len(lines) > 8:
            message += f"\n... 另外 {len(lines) - 8} 个"
        message += "\n\n确认继续？"
        if not messagebox.askyesno("确认撤销求购单", message):
            return

        try:
            config = self.runtime_config()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        order_nos = [order.order_no for order in orders]
        self.set_busy(True)
        self.status.set("正在撤销求购单")
        threading.Thread(
            target=self.run_delete_orders,
            args=(config, self.token.get().strip(), self.device_token.get().strip(), order_nos),
            daemon=True,
        ).start()

    def run_delete_orders(self, config: dict, token: str, device_token: str, order_nos: list[str]) -> None:
        try:
            client = self.make_client(config, token, device_token)
            response = client.delete_purchase_orders(order_nos)
            bidder.ensure_delete_success(response, f"撤销 {len(order_nos)} 个求购单")
            self.queue.put(("log", f"已撤销 {len(order_nos)} 个求购单"))
            orders = market_data.fetch_existing_orders(client)
            self.queue.put(("orders", orders))
            plan = bidder.prepare_cycle_plan(client, config)
            bidder.save_state(config, plan.actions, [], plan.cleanup_actions, [], plan.state)
            self.queue.put(("plan", plan))
            self.queue.put(("status", "撤销完成"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
            self.queue.put(("status", "撤销失败"))
        finally:
            self.queue.put(("busy", False))

    def run_refresh_orders(self, config: dict, token: str, device_token: str) -> None:
        old_log = bidder.log

        def ui_log(message: str) -> None:
            self.queue.put(("log", message))

        bidder.log = ui_log
        try:
            client = self.make_client(config, token, device_token)
            orders = market_data.fetch_existing_orders(client)
            self.queue.put(("orders", orders))
            self.queue.put(("status", "当前求购单已刷新"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
            self.queue.put(("status", "读取求购单失败"))
        finally:
            bidder.log = old_log
            self.queue.put(("busy", False))

    def run_refresh_inventory(self, config: dict, token: str, device_token: str) -> None:
        try:
            client = self.make_client(config, token, device_token)
            state = bidder.load_state(config)
            try:
                orders = market_data.fetch_existing_orders(client)
                bidder.update_purchase_price_fill_events(state, orders, config)
                self.queue.put(("orders", orders))
            except Exception as exc:
                self.queue.put(("log", f"购入价补全：刷新求购状态失败，跳过成交推断：{exc}"))
            inventory = market_data.fetch_inventory_items(client, config, state=state)
            bidder.write_state(config, state)
            market = self.fetch_inventory_market_snapshots(client, inventory, config)
            self.queue.put(("inventory_with_market", (inventory, market)))
            self.queue.put(("status", "库存仓库和盘口已刷新"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
            self.queue.put(("status", "读取库存仓库失败"))
        finally:
            self.queue.put(("busy", False))

    def run_queue_create_commands(self, config: dict, token: str, device_token: str) -> None:
        old_log = bidder.log

        def ui_log(message: str) -> None:
            self.queue.put(("log", message))

        bidder.log = ui_log
        try:
            client = self.make_client(config, token, device_token)
            guard_status = bidder.check_cs_update_guard(client, config, live=False)
            if guard_status.get("halted"):
                orders = market_data.fetch_existing_orders(client)
                plan = bidder.CyclePlan(candidates=[], actions=[], cleanup_actions=[], existing_orders=orders, state={"cs_update_guard": guard_status})
                bidder.save_state(config, [], [], [], [], plan.state)
                self.queue.put(("plan", plan))
                self.queue.put(("orders", orders))
                self.queue.put(("status", "CS update guard halted trading"))
                return
            plan = bidder.prepare_cycle_plan(client, config)
            if plan.actions:
                bidder.enforce_budget(plan.actions, config, plan.state, live=False)
            command_file = self.command_file_path(config)
            queued = execution_bridge.queue_create_commands(plan.actions, command_file, source="desktop")
            payload = execution_bridge.load_command_file(command_file)
            self.queue.put(("plan", plan))
            self.queue.put(("orders", plan.existing_orders))
            self.queue.put(("commands", payload))
            if queued:
                self.queue.put(("log", f"APP command queue updated: {len(queued)} create commands"))
            else:
                self.queue.put(("log", "APP command queue unchanged: no CREATE actions in current plan"))
            self.queue.put(("status", "APP command queue updated"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
            self.queue.put(("status", "APP command queue failed"))
        finally:
            bidder.log = old_log
            self.queue.put(("busy", False))







    def run_cs_update_guard_once(self, config: dict, token: str, device_token: str, live: bool) -> dict:
        old_log = bidder.log

        def ui_log(message: str) -> None:
            self.queue.put(("log", message))

        bidder.log = ui_log
        try:
            client = self.make_client(config, token, device_token)
            status = bidder.check_cs_update_guard(client, config, live=live)
            if status.get("halted"):
                self.queue.put(("status", "CS update guard halted trading"))
            return status
        finally:
            bidder.log = old_log

    def run_daily_report_check_once(self, config: dict, token: str, device_token: str, *, force: bool = False) -> dict:
        client = self.make_client(config, token, device_token)
        status = daily_report.maybe_send_daily_report(client, config, force=force)
        if status.get("sent"):
            self.queue.put(("log", f"日报已发送：{status.get('email_to')} | {status.get('item_count')} 张 | {status.get('total_spend')} 元"))
            self.queue.put(("daily_report_status", f"日报已发送 | {status.get('item_count')} 张 | {status.get('total_spend')} 元"))
        elif status.get("last_error"):
            self.queue.put(("log", f"日报发送失败：{status.get('last_error')}"))
            self.queue.put(("daily_report_status", f"日报失败：{status.get('last_error')}"))
        return status



    def auto_loop(self, config: dict, token: str, device_token: str, execute: bool, live: bool) -> None:
        while not self.auto_stop.is_set():
            self.queue.put(("status", "自动模式：运行一轮"))
            self.run_core(copy.deepcopy(config), token, device_token, execute, live, from_auto=True)
            interval = bidder.refresh_interval_seconds(config)
            self.queue.put(("status", f"自动模式：等待 {interval} 秒"))
            guard = bidder.cs_update_guard_config(config)
            report_config = daily_report.daily_report_config(config)
            alert_config = price_alert.price_alert_config(config)
            elapsed = 0
            while elapsed < interval and not self.auto_stop.is_set():
                wait_seconds = min(
                    interval - elapsed,
                    guard["poll_seconds"] if guard["enabled"] else interval,
                    60 if report_config["enabled"] else interval,
                    alert_config["poll_seconds"] if alert_config["enabled"] else interval,
                )
                if self.auto_stop.wait(wait_seconds):
                    break
                elapsed += wait_seconds
                if guard["enabled"]:
                    try:
                        status = self.run_cs_update_guard_once(config, token, device_token, live)
                        if status.get("halted"):
                            self.queue.put(("log", "CS update guard halted trading; waiting for manual reset."))
                    except Exception as exc:
                        self.queue.put(("log", f"CS update guard check failed: {exc}"))
                if report_config["enabled"]:
                    try:
                        self.run_daily_report_check_once(config, token, device_token)
                    except Exception as exc:
                        self.queue.put(("log", f"daily report check failed: {exc}"))
                if alert_config["enabled"] and elapsed % int(alert_config["poll_seconds"]) == 0:
                    try:
                        self.run_price_alert_check_once(config, token, device_token)
                    except Exception as exc:
                        self.queue.put(("log", f"price alert check failed: {exc}"))
        self.queue.put(("status", "自动模式已停止"))
        self.queue.put(("busy", False))

    def run_core(self, config: dict, token: str, device_token: str, execute: bool, live: bool, from_auto: bool = False) -> None:
        old_log = bidder.log

        def ui_log(message: str) -> None:
            self.queue.put(("log", message))

        bidder.log = ui_log
        try:
            client = self.make_client(config, token, device_token)
            guard_status = bidder.check_cs_update_guard(client, config, live=live)
            if guard_status.get("halted"):
                orders = market_data.fetch_existing_orders(client)
                plan = bidder.CyclePlan(candidates=[], actions=[], cleanup_actions=[], existing_orders=orders, state={"cs_update_guard": guard_status})
                bidder.save_state(config, [], [], [], [], plan.state)
                self.queue.put(("plan", plan))
                self.queue.put(("orders", orders))
                self.queue.put(("status", "CS update guard halted trading"))
                return
            plan = bidder.prepare_cycle_plan(client, config)
            if plan.actions:
                bidder.enforce_budget(plan.actions, config, plan.state, live=live and not config.get("dry_run", True))
            submitted: list[bidder.PlanAction] = []
            submitted_cleanup: list[bidder.CleanupAction] = []
            if execute and plan.cleanup_actions:
                submitted_cleanup = bidder.execute_cleanup_plan(client, plan.cleanup_actions, config, live=live)
                plan = bidder.prepare_cycle_plan(client, config)
            if execute and plan.actions:
                submitted = bidder.execute_plan(client, plan.actions, config, live=live, state=plan.state)
                self.process_create_orders_inline(config, live=live)
                plan = bidder.prepare_cycle_plan(client, config)
            bidder.save_state(config, plan.actions, submitted, plan.cleanup_actions, submitted_cleanup, plan.state)
            self.queue.put(("commands", execution_bridge.load_command_file(self.command_file_path(config))))
            self.queue.put(("plan", plan))
            self.queue.put(("orders", plan.existing_orders))
            try:
                inventory = market_data.fetch_inventory_items(client, config)
                market = self.fetch_inventory_market_snapshots(client, inventory, config)
                self.queue.put(("inventory_with_market", (inventory, market)))
            except Exception as exc:
                self.queue.put(("log", f"库存仓库刷新失败：{exc}"))
            try:
                if price_alert.price_alert_config(config)["enabled"]:
                    self.run_price_alert_check_once(config, token, device_token)
            except Exception as exc:
                self.queue.put(("log", f"价格预警检查失败：{exc}"))
            try:
                exit_actions = bidder.prepare_exit_actions(client, config)
                submitted_exit_actions: list[bidder.ExitAction] = []
                if execute and config.get("exit_strategy", {}).get("auto_execute", False):
                    submitted_exit_actions = bidder.execute_exit_actions(client, exit_actions, config, live=live)
                    exit_actions = bidder.prepare_exit_actions(client, config)
                self.queue.put(("exit_actions", exit_actions))
                if submitted_exit_actions:
                    self.queue.put(("log", f"自动出货已执行 {len(submitted_exit_actions)} 个动作"))
            except Exception as exc:
                self.queue.put(("log", f"自动卖出状态刷新失败：{exc}"))
            self.queue.put(("status", "实盘完成" if execute else "预览完成"))
        except Exception as exc:
            self.queue.put(("error", str(exc)))
            self.queue.put(("status", "出错"))
        finally:
            bidder.log = old_log
            if not from_auto:
                self.queue.put(("busy", False))

    def update_plan_table(self, plan: bidder.CyclePlan) -> None:
        for row in self.plan_tree.get_children():
            self.plan_tree.delete(row)
        self.current_actions_by_iid.clear()
        for idx, action in enumerate(plan.actions, start=1):
            existing = action.existing_order
            tag = "positive" if action.action == "update" else "blue" if action.action == "create" else "muted" if action.action.startswith("skip") else "purple"
            iid = self.plan_tree.insert(
                "",
                tk.END,
                tags=(tag,),
                values=(
                    idx,
                    action.action,
                    action.template.name,
                    str(action.reference_price or ""),
                    str(action.bid_price),
                    action.purchase_num,
                    money_text(action.spend),
                    existing.rank if existing and existing.rank is not None else "",
                    str(existing.max_purchase_price) if existing and existing.max_purchase_price is not None else "",
                    action.template.raw.get("_selection_reason", ""),
                    existing.order_no if existing else "",
                ),
            )
            self.current_actions_by_iid[str(iid)] = action
        self.summary.set(
            f"计划总金额：{money_text(plan.active_spend)}    "
            f"本轮新增风险：{money_text(plan.new_exposure)}    "
            f"品类数：{len(plan.actions)}"
        )
        cleanup_actions = getattr(plan, "cleanup_actions", [])
        if cleanup_actions:
            self.summary.set(self.summary.get() + f"    待清理：{len(cleanup_actions)}")
        purchase_risk = plan.state.get("purchase_risk") if isinstance(plan.state, dict) else None
        if isinstance(purchase_risk, dict) and purchase_risk.get("enabled"):
            window_start = purchase_risk.get("window_start") or "当天 00:00"
            self.summary.set(
                self.summary.get()
                + "    "
                + f"日内成交：{purchase_risk.get('recent_success_spend', '0')}/"
                + f"{purchase_risk.get('rolling_success_limit', '0')}    "
                + f"剩余：{purchase_risk.get('remaining_success_budget', '0')}    "
                + f"挂单上限：{purchase_risk.get('active_order_exposure_limit', '0')}"
            )
            self.append_log(
                "日内成交风控："
                f"起算 {window_start}，"
                f"成交 {purchase_risk.get('recent_success_spend', '0')}，"
                f"剩余 {purchase_risk.get('remaining_success_budget', '0')}，"
                f"活跃求购风险 {purchase_risk.get('active_order_spend_after_plan', '0')}，"
                f"挂单上限 {purchase_risk.get('active_order_exposure_limit', '0')}"
            )

    def update_orders_table(self, orders: dict[int, bidder.ExistingPurchaseOrder]) -> None:
        for row in self.orders_tree.get_children():
            self.orders_tree.delete(row)
        self.current_orders_by_iid.clear()
        sorted_orders = sorted(
            orders.values(),
            key=lambda order: (order.rank if order.rank is not None else 999999, order.commodity_name),
        )
        active_spend = Decimal("0")
        for idx, order in enumerate(sorted_orders, start=1):
            spend = order.spend
            active_spend += spend
            tag = "positive" if order.rank is not None and order.rank <= 2 else "warning" if order.rank is not None and order.rank <= 5 else "muted"
            iid = self.orders_tree.insert(
                "",
                tk.END,
                tags=(tag,),
                values=(
                    idx,
                    order.commodity_name,
                    str(order.purchase_price or ""),
                    order.purchase_num or "",
                    order.rank if order.rank is not None else "",
                    str(order.max_purchase_price) if order.max_purchase_price is not None else "",
                    money_text(spend),
                    order.order_no,
                ),
            )
            self.current_orders_by_iid[str(iid)] = order
        self.order_summary.set(f"当前有效求购单：{len(sorted_orders)}    已有求购金额：{money_text(active_spend)}")

    def update_commands_table(self, payload: dict) -> None:
        trees = []
        if hasattr(self, "commands_tree"):
            trees.append((self.commands_tree, self.current_commands_by_iid, True))
        if hasattr(self, "workbench_commands_tree"):
            trees.append((self.workbench_commands_tree, self.current_workbench_commands_by_iid, False))
        if not trees:
            return
        for tree, mapping, _full in trees:
            for row in tree.get_children():
                tree.delete(row)
            mapping.clear()

        commands = payload.get("commands", [])
        if not isinstance(commands, list):
            commands = []
        counts = execution_bridge.command_counts(payload)
        self.command_summary.set(
            "APP commands: "
            f"pending {counts.get('pending', 0)} | "
            f"done {counts.get('done', 0)} | "
            f"failed {counts.get('failed', 0)} | "
            f"total {len(commands)}"
        )
        for idx, command in enumerate(commands, start=1):
            if not isinstance(command, dict):
                continue
            status = str(command.get("status", "unknown"))
            tag = "blue" if status == "pending" else "positive" if status == "done" else "danger" if status == "failed" else "muted"
            price_text = str(command.get("bid_price", command.get("buy_price", "")))
            qty_text = str(command.get("purchase_num", command.get("quantity", "")))
            amount_text = str(command.get("amount", ""))
            full_values = (
                idx,
                status,
                str(command.get("type", "")),
                str(command.get("commodity_name", command.get("template_hash_name", ""))),
                price_text,
                qty_text,
                amount_text,
                str(command.get("source", "")),
                str(command.get("created_at", "")),
                str(command.get("id", "")),
            )
            compact_values = (
                idx,
                status,
                str(command.get("commodity_name", command.get("template_hash_name", ""))),
                price_text,
                qty_text,
                amount_text,
                str(command.get("source", "")),
                str(command.get("created_at", "")),
            )
            for tree, mapping, full in trees:
                iid = tree.insert("", tk.END, tags=(tag,), values=full_values if full else compact_values)
                mapping[str(iid)] = command

    def update_inventory_table(
        self,
        inventory: list[bidder.InventoryItem],
        market: dict[str, bidder.MarketSnapshot] | None = None,
    ) -> None:
        if market is not None:
            self.current_inventory_market_by_key = market
        selected_asset_ids = {
            self.current_inventory_by_iid[str(iid)].asset_id
            for iid in self.inventory_tree.selection()
            if str(iid) in self.current_inventory_by_iid
        }
        for row in self.inventory_tree.get_children():
            self.inventory_tree.delete(row)
        self.current_inventory_by_iid.clear()

        counts: dict[str, int] = {}
        for item in inventory:
            key = item.market_hash_name or item.name
            counts[key] = counts.get(key, 0) + 1

        sorted_inventory = sorted(
            inventory,
            key=lambda item: (
                0 if item.status_text == "CD中" else 1 if item.status_text == "可交易" else 2,
                item.name,
                item.asset_id,
            ),
        )

        total_buy = Decimal("0")
        total_reference = Decimal("0")
        cooling_count = 0
        tradable_count = 0
        untradable_count = 0
        rule_map = self.exit_rule_map()
        alert_map = self.price_alert_rule_map()
        for idx, item in enumerate(sorted_inventory, start=1):
            if item.buy_price is not None:
                total_buy += item.buy_price
            if item.reference_price is not None:
                total_reference += item.reference_price
            if item.status_text == "CD中":
                cooling_count += 1
            elif item.status_text == "可交易":
                tradable_count += 1
            else:
                untradable_count += 1

            key = item.market_hash_name or item.name
            snapshot = self.current_inventory_market_by_key.get(key)
            highest_bid = snapshot.highest_purchase_price if snapshot else None
            lowest_sell = snapshot.lowest_sell_price if snapshot else None
            rule = rule_map.get(key)
            auto_price = ""
            exit_mode = ""
            if rule:
                rule_price = exit_manager.rule_target(rule)
                if rule_price is not None:
                    auto_price = money_text(rule_price)
                exit_mode = self.exit_mode_label(exit_manager.rule_action_mode(rule))
            alert_rule = alert_map.get(key)
            bid_alert_price = ""
            sale_alert_price = ""
            if alert_rule:
                parsed_bid_alert_price, parsed_sale_alert_price = price_alert.rule_targets(alert_rule)
                if parsed_bid_alert_price is not None:
                    bid_alert_price = money_text(parsed_bid_alert_price)
                if parsed_sale_alert_price is not None:
                    sale_alert_price = money_text(parsed_sale_alert_price)
            tag = "warning" if item.status_text == "CD中" else "positive" if item.status_text == "可交易" else "danger"
            iid = self.inventory_tree.insert(
                "",
                tk.END,
                tags=(tag,),
                values=(
                    idx,
                    item.name,
                    counts.get(key, 1),
                    money_text(item.buy_price) if item.buy_price is not None else "",
                    money_text(highest_bid) if highest_bid is not None else "",
                    money_text(lowest_sell) if lowest_sell is not None else "",
                    self.spread_text(snapshot),
                    auto_price,
                    bid_alert_price,
                    sale_alert_price,
                    exit_mode,
                    money_text(item.reference_price) if item.reference_price is not None else item.price_desc,
                    item.display_cooldown,
                    item.status_text,
                    item.asset_id,
                ),
            )
            self.current_inventory_by_iid[str(iid)] = item
            if item.asset_id in selected_asset_ids:
                self.inventory_tree.selection_add(iid)

        self.inventory_summary.set(
            f"库存资产：{len(sorted_inventory)}    "
            f"CD中：{cooling_count}    可交易：{tradable_count}    不可交易：{untradable_count}    "
            f"购入成本：{money_text(total_buy)}    参考市值：{money_text(total_reference)}"
        )
        self.update_exit_rule_summary_from_config()
        self.on_inventory_select()

    def update_exit_table(self, actions: list[bidder.ExitAction]) -> None:
        self.current_exit_actions_by_iid.clear()

        actionable_count = 0
        rule_keys: set[str] = set()
        for idx, action in enumerate(actions, start=1):
            snapshot = action.snapshot
            if action.is_actionable:
                actionable_count += 1
            key = str(action.rule.get("market_hash_name", action.rule.get("name", action.item.name)))
            rule_keys.add(key)
            self.current_exit_actions_by_iid[str(idx)] = action
        self.exit_summary.set(f"自动卖出规则：{len(rule_keys)}    检查库存：{len(actions)}    达标动作：{actionable_count}")

    def drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.append_log(str(payload))
                elif kind == "plan":
                    self.update_plan_table(payload)  # type: ignore[arg-type]
                elif kind == "orders":
                    self.update_orders_table(payload)  # type: ignore[arg-type]
                elif kind == "inventory":
                    self.update_inventory_table(payload)  # type: ignore[arg-type]
                elif kind == "inventory_with_market":
                    inventory, market = payload  # type: ignore[misc]
                    self.update_inventory_table(inventory, market)
                elif kind == "inventory_market":
                    self.current_inventory_market_by_key.update(payload)  # type: ignore[arg-type]
                    self.update_inventory_table(list(self.current_inventory_by_iid.values()))
                elif kind == "exit_actions":
                    self.update_exit_table(payload)  # type: ignore[arg-type]
                elif kind == "commands":
                    self.update_commands_table(payload)  # type: ignore[arg-type]
                elif kind == "emulator_status":
                    self.update_emulator_status_text(payload)  # type: ignore[arg-type]
                elif kind == "daily_report_status":
                    self.daily_report_status.set(str(payload))
                elif kind == "price_alert_status":
                    self.price_alert_status.set(str(payload))
                elif kind == "error":
                    self.append_log(str(payload))
                    messagebox.showerror("运行失败", str(payload))
                elif kind == "status":
                    self.status.set(str(payload))
                elif kind == "busy":
                    self.set_busy(bool(payload))
        except queue.Empty:
            pass
        self.root.after(200, self.drain_queue)


def main() -> None:
    root = tk.Tk()
    YouPinDesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
