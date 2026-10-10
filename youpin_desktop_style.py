from __future__ import annotations

import tkinter as tk
from tkinter import ttk


THEME = {
    "bg": "#0b1118",
    "surface": "#111827",
    "surface_2": "#151f2e",
    "surface_3": "#1f2937",
    "line": "#263244",
    "text": "#e5e7eb",
    "muted": "#94a3b8",
    "muted_2": "#64748b",
    "green": "#22c55e",
    "green_soft": "#0f3b2e",
    "red": "#ef4444",
    "red_soft": "#40202a",
    "yellow": "#f59e0b",
    "blue": "#38bdf8",
    "purple": "#a78bfa",
}


def configure_desktop_style(root: tk.Tk) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg=THEME["bg"])
    style.configure(".", font=("Microsoft YaHei UI", 9))
    style.configure("TFrame", background=THEME["bg"])
    style.configure(
        "Panel.TFrame",
        background=THEME["surface"],
        relief="solid",
        borderwidth=1,
        bordercolor=THEME["line"],
        lightcolor=THEME["line"],
        darkcolor=THEME["line"],
    )
    style.configure("Sidebar.TFrame", background=THEME["surface"])
    style.configure(
        "Section.TFrame",
        background=THEME["surface_2"],
        relief="solid",
        borderwidth=1,
        bordercolor=THEME["line"],
        lightcolor=THEME["line"],
        darkcolor=THEME["line"],
    )
    style.configure(
        "Metric.TFrame",
        background=THEME["surface"],
        relief="solid",
        borderwidth=1,
        bordercolor=THEME["line"],
        lightcolor=THEME["line"],
        darkcolor=THEME["line"],
    )
    style.configure("Header.TFrame", background=THEME["bg"])
    style.configure("Header.TLabel", background=THEME["bg"], foreground=THEME["text"], font=("Microsoft YaHei UI", 18, "bold"))
    style.configure("HeaderSub.TLabel", background=THEME["bg"], foreground=THEME["muted"])
    style.configure("Title.TLabel", background=THEME["surface"], foreground=THEME["text"], font=("Microsoft YaHei UI", 10, "bold"))
    style.configure("SectionTitle.TLabel", background=THEME["surface"], foreground=THEME["muted"], font=("Microsoft YaHei UI", 9, "bold"))
    style.configure("Muted.TLabel", background=THEME["surface"], foreground=THEME["muted"])
    style.configure("SectionMuted.TLabel", background=THEME["surface_2"], foreground=THEME["muted"])
    style.configure("Status.TLabel", background=THEME["surface_2"], foreground=THEME["green"], font=("Microsoft YaHei UI", 10, "bold"))
    style.configure("MetricTitle.TLabel", background=THEME["surface"], foreground=THEME["muted"], font=("Microsoft YaHei UI", 8, "bold"))
    style.configure("MetricValue.TLabel", background=THEME["surface"], foreground=THEME["text"], font=("Microsoft YaHei UI", 10, "bold"))

    style.configure("TCheckbutton", background=THEME["surface_2"], foreground=THEME["text"])
    style.map("TCheckbutton", background=[("active", THEME["surface_2"])], foreground=[("disabled", THEME["muted_2"])])
    style.configure(
        "TEntry",
        fieldbackground=THEME["bg"],
        foreground=THEME["text"],
        insertcolor=THEME["text"],
        bordercolor=THEME["line"],
        lightcolor=THEME["line"],
        darkcolor=THEME["line"],
    )
    style.configure(
        "TCombobox",
        fieldbackground=THEME["bg"],
        background=THEME["surface_3"],
        foreground=THEME["text"],
        arrowcolor=THEME["text"],
        bordercolor=THEME["line"],
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", THEME["surface_3"]), ("disabled", THEME["surface_2"])],
        foreground=[("readonly", THEME["text"]), ("disabled", THEME["muted_2"])],
        selectbackground=[("readonly", THEME["surface_3"])],
        selectforeground=[("readonly", THEME["text"])],
    )

    style.configure("TButton", background=THEME["surface_3"], foreground=THEME["text"], borderwidth=0, focusthickness=0, padding=(10, 7))
    style.map("TButton", background=[("active", "#2b394d"), ("disabled", "#172033")], foreground=[("disabled", THEME["muted_2"])])
    style.configure("Primary.TButton", background=THEME["green"], foreground="#04130c", font=("Microsoft YaHei UI", 9, "bold"))
    style.map("Primary.TButton", background=[("active", "#34d399"), ("disabled", "#172033")])
    style.configure("Danger.TButton", background=THEME["red"], foreground="#160404", font=("Microsoft YaHei UI", 9, "bold"))
    style.map("Danger.TButton", background=[("active", "#fb7185"), ("disabled", "#172033")])

    scrollbar_options = {
        "background": THEME["surface_3"],
        "troughcolor": THEME["surface"],
        "bordercolor": THEME["surface"],
        "arrowcolor": THEME["muted"],
        "darkcolor": THEME["surface_3"],
        "lightcolor": THEME["surface_3"],
        "relief": tk.FLAT,
        "borderwidth": 0,
    }
    style.configure("TScrollbar", **scrollbar_options)
    style.configure("Vertical.TScrollbar", **scrollbar_options)
    style.configure("Horizontal.TScrollbar", **scrollbar_options)
    style.map("TScrollbar", background=[("active", "#2b394d")])
    style.map("Vertical.TScrollbar", background=[("active", "#2b394d")])
    style.map("Horizontal.TScrollbar", background=[("active", "#2b394d")])

    style.configure("TNotebook", background=THEME["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", background=THEME["surface"], foreground=THEME["muted"], padding=(16, 8), borderwidth=0)
    style.map(
        "TNotebook.Tab",
        background=[("selected", THEME["surface_3"]), ("active", THEME["surface_2"])],
        foreground=[("selected", THEME["text"]), ("active", THEME["text"])],
    )

    style.configure(
        "Treeview",
        background=THEME["surface"],
        fieldbackground=THEME["surface"],
        foreground=THEME["text"],
        rowheight=28,
        borderwidth=0,
        font=("Microsoft YaHei UI", 9),
    )
    style.configure("Treeview.Heading", background=THEME["surface_3"], foreground=THEME["muted"], borderwidth=0, font=("Microsoft YaHei UI", 9, "bold"))
    style.map("Treeview", background=[("selected", "#21415c")], foreground=[("selected", "#ffffff")])
