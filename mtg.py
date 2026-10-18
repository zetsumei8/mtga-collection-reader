"""MTGA Collection Reader 5.2.5.

Normal use is one click: the app installs and starts an independent read-only
Arena helper, reads the complete owned-card snapshot, saves it, and stops the
helper. No screenshots, scrolling, OCR, Player.log parsing, or anchor cards are
required. Manual JSON remains the permanent fallback.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from bridge_service import run_server
from card_database import collection_observations, prepare_database
from collection_store import CollectionStore, MergeSummary, SuspiciousSnapshotError
from exports import write_collection_exports
from manual_import import ManualImportError, load_manual_snapshot
from daemon_provider import DaemonProviderError, MtgaDaemonProvider
from provider_contract import CollectionSnapshot

APP_NAME = "MTGA Collection Reader"
APP_VERSION = "5.2.5"
DEFAULT_RAW_FOLDER = Path(r"G:\MTGA\MTGA_Data\Downloads\Raw")

BG = "#0C121B"
SIDEBAR = "#091019"
PANEL = "#131D29"
PANEL_2 = "#1A2734"
BORDER = "#2A3A4A"
TEXT = "#EAF1F4"
MUTED = "#9AAAB7"
ACCENT = "#77DCCB"
GOOD = "#62D39B"
WARN = "#E8B86D"
DANGER = "#EC7C85"
PURPLE = "#A991F4"


class CancelledError(RuntimeError):
    pass


def app_data_dir() -> Path:
    configured = os.environ.get("MTGA_READER_DATA_DIR") or os.environ.get("MTGA_EXPORTER_DATA_DIR")
    if configured:
        return Path(configured)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / APP_NAME
    return Path.home() / ".mtga_collection_reader"


def app_output_dir() -> Path:
    configured = os.environ.get("MTGA_READER_OUTPUT_DIR") or os.environ.get("MTGA_EXPORTER_OUTPUT_DIR")
    if configured:
        return Path(configured)
    return app_data_dir() / "Exports"


def open_path(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        messagebox.showerror(APP_NAME, f"Could not open {path}:\n{exc}")


class Settings:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.values: Dict[str, Any] = {
            "arena_raw_path": str(DEFAULT_RAW_FOLDER),
            "output_path": str(app_output_dir()),
            "helper_auto_install": True,
        }
        self.load()

    def load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self.values.update(payload)
        except (OSError, ValueError, TypeError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.values, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value


class Reporter:
    def __init__(self, events: "queue.Queue[tuple[str, Dict[str, Any]]]", cancel_event: threading.Event) -> None:
        self.events = events
        self.cancel_event = cancel_event

    def emit(self, event_type: str, **payload: Any) -> None:
        self.events.put((event_type, payload))

    def log(self, message: str, level: str = "INFO") -> None:
        self.emit("log", timestamp=time.strftime("%H:%M:%S"), level=level, message=str(message))

    def status(self, message: str, detail: str = "") -> None:
        self.emit("status", title=message, detail=detail, kind="normal")

    def stage(self, number: int, total: int, name: str, detail: str = "", mode: str = "determinate") -> None:
        self.emit("stage", number=number, total=total, name=name, detail=detail, mode=mode)

    def progress(self, current: int, total: int, label: str, _eta: Optional[float] = None) -> None:
        percent = 0.0 if total <= 0 else max(0.0, min(100.0, current * 100.0 / total))
        self.emit("progress", percent=percent, label=label)

    def overall(self, fraction: float) -> None:
        self.emit("progress", percent=max(0.0, min(100.0, float(fraction) * 100.0)), label="")

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise CancelledError("Operation cancelled.")


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1200x790")
        self.minsize(1000, 680)
        self.configure(bg=BG)

        self.data_dir = app_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = Settings(self.data_dir / "settings.json")
        self.store = CollectionStore(self.data_dir / "collection.db")
        self.events: "queue.Queue[tuple[str, Dict[str, Any]]]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.reporter = Reporter(self.events, self.cancel_event)
        self.worker: Optional[threading.Thread] = None
        self.pending_snapshot: Optional[tuple[CollectionSnapshot, list[dict[str, Any]], int, Dict[str, Any]]] = None
        self.bridge_thread: Optional[threading.Thread] = None
        self.current_page = "scan"
        self.arena_running: Optional[bool] = None
        self.activity_open = False
        self._closing = False

        self._configure_style()
        self._build_shell()
        self._show_page("scan")
        self._refresh_all()
        self.after(80, self._poll_events)
        self.after(250, self._schedule_arena_presence)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Segoe UI", 10), background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL, relief="flat", borderwidth=0)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Card.TLabel", background=PANEL, foreground=TEXT)
        style.configure("CardMuted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 25, "bold"))
        style.configure("Metric.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI", 21, "bold"))
        style.configure("TButton", background=PANEL_2, foreground=TEXT, padding=(12, 9), borderwidth=0, relief="flat", font=("Segoe UI", 9, "bold"))
        style.map("TButton", background=[("disabled", PANEL), ("pressed", "#243846"), ("active", "#26394A")], foreground=[("disabled", "#687887"), ("active", TEXT)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#09201D", padding=(16, 11), borderwidth=0, relief="flat", font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton", background=[("disabled", BORDER), ("pressed", "#57BDAF"), ("active", "#8BE9D9")], foreground=[("disabled", MUTED), ("active", "#071B18")])
        style.configure("Subtle.TButton", background=PANEL, foreground=MUTED, padding=(10, 8), borderwidth=0, relief="flat", font=("Segoe UI", 9, "bold"))
        style.map("Subtle.TButton", background=[("pressed", PANEL_2), ("active", PANEL_2)], foreground=[("active", TEXT)])
        style.configure("Danger.TButton", background="#49252D", foreground="#FFDCE0", padding=(12, 9), borderwidth=0, relief="flat", font=("Segoe UI", 9, "bold"))
        style.map("Danger.TButton", background=[("disabled", PANEL), ("pressed", "#6A303B"), ("active", "#63303A")], foreground=[("disabled", "#687887")])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=31, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=PANEL_2, foreground=MUTED, font=("Segoe UI", 9, "bold"), padding=(8, 9), relief="flat")
        style.map("Treeview.Heading", background=[("active", "#26394A")], foreground=[("active", TEXT)])
        style.map("Treeview", background=[("selected", "#24444A")], foreground=[("selected", TEXT)])
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT, padding=(9, 8), borderwidth=0)
        style.map("TEntry", fieldbackground=[("focus", "#202F3E")])
        style.configure("TCombobox", fieldbackground=PANEL_2, foreground=TEXT)
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT)
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL_2, bordercolor=PANEL_2, lightcolor=ACCENT, darkcolor=ACCENT)

    def _build_shell(self) -> None:
        sidebar = tk.Frame(self, bg=SIDEBAR, width=218)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=20, pady=(22, 24))
        mark = tk.Canvas(brand, width=38, height=38, bg=SIDEBAR, highlightthickness=0)
        mark.pack(side="left", padx=(0, 11))
        mark.create_polygon(19, 2, 35, 11, 35, 27, 19, 36, 3, 27, 3, 11, fill="#17342F", outline=ACCENT, width=1)
        mark.create_polygon(19, 8, 27, 19, 19, 30, 11, 19, fill=ACCENT, outline="")
        brand_copy = tk.Frame(brand, bg=SIDEBAR)
        brand_copy.pack(side="left", fill="y")
        tk.Label(brand_copy, text="MTGA", bg=SIDEBAR, fg=TEXT, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(brand_copy, text="COLLECTION VAULT", bg=SIDEBAR, fg=MUTED, font=("Segoe UI", 7, "bold")).pack(anchor="w", pady=(1, 0))
        tk.Frame(sidebar, bg=BORDER, height=1).pack(fill="x", padx=18)
        tk.Label(sidebar, text="WORKSPACE", bg=SIDEBAR, fg="#718394", font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=20, pady=(20, 8))
        self.nav_buttons: Dict[str, tk.Button] = {}
        self.nav_indicators: Dict[str, tk.Frame] = {}
        for key, label, icon in (("scan", "Sync", "◉"), ("collection", "Collection", "▤"), ("history", "Snapshots", "↻"), ("settings", "Settings", "⚙")):
            row = tk.Frame(sidebar, bg=SIDEBAR, height=44)
            row.pack(fill="x", padx=10, pady=2)
            row.pack_propagate(False)
            indicator = tk.Frame(row, bg=SIDEBAR, width=3)
            indicator.pack(side="left", fill="y")
            button = tk.Button(
                row, text=f"{icon}   {label}", command=lambda page=key: self._show_page(page), bg=SIDEBAR, fg=MUTED,
                activebackground=PANEL_2, activeforeground=TEXT, bd=0, relief="flat", anchor="w", padx=14,
                pady=10, font=("Segoe UI", 10, "bold"), cursor="hand2", highlightthickness=0,
            )
            button.pack(side="left", fill="both", expand=True)
            button.bind("<Enter>", lambda _event, page=key: self._set_nav_hover(page, True))
            button.bind("<Leave>", lambda _event, page=key: self._set_nav_hover(page, False))
            self.nav_buttons[key] = button
            self.nav_indicators[key] = indicator

        tk.Frame(sidebar, bg=BORDER, height=1).pack(fill="x", padx=18, pady=(18, 0))
        status_card = tk.Frame(sidebar, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        status_card.pack(side="bottom", fill="x", padx=12, pady=(12, 10))
        status_head = tk.Frame(status_card, bg=PANEL)
        status_head.pack(fill="x", padx=12, pady=(11, 2))
        self.arena_dot = tk.Canvas(status_head, width=11, height=11, bg=PANEL, highlightthickness=0)
        self.arena_dot.pack(side="left", padx=(0, 8))
        self.arena_dot.create_oval(2, 2, 9, 9, fill=WARN, outline="")
        self.arena_status_label = tk.Label(status_head, text="CHECKING ARENA", bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "bold"), anchor="w")
        self.arena_status_label.pack(side="left", fill="x", expand=True)
        self.arena_status_hint = tk.Label(status_card, text="Open Arena to enable sync", bg=PANEL, fg=MUTED, font=("Segoe UI", 8), anchor="w", wraplength=170, justify="left")
        self.arena_status_hint.pack(fill="x", padx=12, pady=(0, 11))
        tk.Label(sidebar, text=f"VERSION {APP_VERSION}", bg=SIDEBAR, fg="#718394", font=("Segoe UI", 8, "bold")).pack(side="bottom", anchor="w", padx=20, pady=(0, 12))

        self.content = tk.Frame(self, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)
        topbar = tk.Frame(self.content, bg=BG, height=58)
        topbar.pack(fill="x", padx=28, pady=(12, 0))
        topbar.pack_propagate(False)
        self.topbar_title = tk.Label(topbar, text="COLLECTION WORKSPACE", bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold"), anchor="w")
        self.topbar_title.pack(side="left", fill="y")
        tk.Label(topbar, text="LOCAL  /  PRIVATE", bg=BG, fg="#708697", font=("Segoe UI", 8, "bold")).pack(side="right", fill="y")
        tk.Frame(self.content, bg=BORDER, height=1).pack(fill="x", padx=28)
        self.pages = {
            "scan": self._build_scan_page(),
            "collection": self._build_collection_page(),
            "history": self._build_history_page(),
            "settings": self._build_settings_page(),
        }

    def _set_nav_hover(self, key: str, hovered: bool) -> None:
        button = self.nav_buttons.get(key)
        if button is None:
            return
        if key == self.current_page:
            bg, fg = PANEL, TEXT
        elif hovered:
            bg, fg = PANEL_2, TEXT
        else:
            bg, fg = SIDEBAR, MUTED
        button.configure(bg=bg, fg=fg)

    def _page_header(self, parent: tk.Widget, title: str, subtitle: str) -> None:
        ttk.Label(parent, text=title, style="Title.TLabel").pack(anchor="w")
        description = ttk.Label(parent, text=subtitle, style="Muted.TLabel", wraplength=920, justify="left")
        description.pack(anchor="w", pady=(3, 18))
        parent.bind(
            "<Configure>",
            lambda event, label=description: label.configure(wraplength=max(320, event.width - 8)),
            add="+",
        )

    def _build_scan_page(self) -> tk.Frame:
        page = tk.Frame(self.content, bg=BG)
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=30, pady=22)
        self._page_header(
            body,
            "Your Arena collection",
            "A complete local snapshot, ready to search and export. Open MTG Arena, sign in, and wait at the Home screen before syncing.",
        )

        metrics = tk.Frame(body, bg=BG)
        metrics.pack(fill="x")
        self.metric_labels: Dict[str, ttk.Label] = {}
        for index, (key, label) in enumerate((
            ("owned_copies", "OWNED COPIES"),
            ("unique_entries", "UNIQUE ARENA IDS"),
            ("needs_review", "NEEDS REVIEW"),
        )):
            card = tk.Frame(metrics, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            card.pack(side="left", fill="x", expand=True, padx=(0, 10 if index < 2 else 0))
            tk.Frame(card, bg=(ACCENT if index == 0 else (PURPLE if index == 1 else WARN)), height=2).pack(fill="x")
            inner = tk.Frame(card, bg=PANEL)
            inner.pack(fill="both", expand=True, padx=16, pady=(10, 12))
            value = ttk.Label(inner, text="0", style="Metric.TLabel")
            value.pack(anchor="w")
            ttk.Label(inner, text=label, style="CardMuted.TLabel", font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(2, 0))
            self.metric_labels[key] = value

        source_row = tk.Frame(body, bg=BG)
        source_row.pack(fill="x", expand=True, pady=(16, 0))

        automatic = tk.Frame(source_row, bg=PANEL, highlightthickness=1, highlightbackground="#326159")
        automatic.pack(side="left", fill="both", expand=True, padx=(0, 8))
        auto_inner = tk.Frame(automatic, bg=PANEL)
        auto_inner.pack(fill="both", expand=True, padx=20, pady=18)
        auto_top = tk.Frame(auto_inner, bg=PANEL)
        auto_top.pack(fill="x")
        ttk.Label(auto_top, text="01  /  LIVE CAPTURE", style="CardMuted.TLabel", font=("Segoe UI", 8, "bold")).pack(side="left")
        tk.Label(auto_top, text="LOCAL ONLY", bg="#18362F", fg=ACCENT, padx=8, pady=3, font=("Segoe UI", 7, "bold")).pack(side="right")
        ttk.Label(auto_inner, text="Automatic sync", style="Card.TLabel", font=("Segoe UI", 17, "bold")).pack(anchor="w", pady=(12, 0))
        self.auto_description = ttk.Label(
            auto_inner,
            text="Read your full owned-card snapshot directly from the running Arena client. The verified helper stays on this PC and closes when the read is done.",
            style="CardMuted.TLabel", wraplength=470, justify="left",
        )
        self.auto_description.pack(anchor="w", pady=(7, 12))
        step_row = tk.Frame(auto_inner, bg=PANEL)
        step_row.pack(fill="x", pady=(0, 14))
        for label in ("1  Open Arena", "2  Reach Home", "3  Sync"):
            tk.Label(step_row, text=label, bg=PANEL_2, fg=TEXT, padx=8, pady=6, font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 6))
        self.memory_button = ttk.Button(
            auto_inner, text="Sync my collection   →", style="Accent.TButton", command=self._start_memory
        )
        self.memory_button.pack(anchor="w")
        self._bind_card_hover(automatic, "#326159", "#4B8A7E")

        manual = tk.Frame(source_row, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        manual.pack(side="left", fill="both", expand=True, padx=(8, 0))
        manual_inner = tk.Frame(manual, bg=PANEL)
        manual_inner.pack(fill="both", expand=True, padx=20, pady=18)
        ttk.Label(manual_inner, text="02  /  FILE IMPORT", style="CardMuted.TLabel", font=("Segoe UI", 8, "bold")).pack(anchor="w")
        ttk.Label(manual_inner, text="Manual snapshot", style="Card.TLabel", font=("Segoe UI", 17, "bold")).pack(anchor="w", pady=(12, 0))
        self.manual_description = ttk.Label(
            manual_inner,
            text="Import a saved Arena JSON snapshot. Empty or suspiciously smaller files are checked before they can replace your collection.",
            style="CardMuted.TLabel", wraplength=440, justify="left",
        )
        self.manual_description.pack(anchor="w", pady=(7, 14))
        self.manual_path_var = tk.StringVar()
        row = tk.Frame(manual_inner, bg=PANEL)
        row.pack(fill="x", pady=(1, 0))
        ttk.Entry(row, textvariable=self.manual_path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", style="Subtle.TButton", command=self._browse_manual).pack(side="left", padx=(8, 0))
        self.manual_button = ttk.Button(manual_inner, text="Import snapshot", command=self._start_manual_import)
        self.manual_button.pack(anchor="w", pady=(12, 0))
        self._bind_card_hover(manual, BORDER, "#435B70")

        source_row.bind("<Configure>", self._resize_scan_copy, add="+")

        self.banner = tk.Frame(body, bg=PANEL_2, highlightthickness=1, highlightbackground=BORDER)
        self.banner.pack(fill="x", pady=(15, 0))
        banner_left = tk.Frame(self.banner, bg=PANEL_2)
        banner_left.pack(side="left", fill="both", expand=True, padx=16, pady=12)
        self.banner_title = tk.Label(banner_left, text="READY FOR YOUR NEXT SNAPSHOT", bg=PANEL_2, fg=ACCENT, font=("Segoe UI", 10, "bold"), anchor="w")
        self.banner_title.pack(fill="x")
        self.banner_detail = tk.Label(banner_left, text="Arena must be open at the Home screen for automatic sync.", bg=PANEL_2, fg=MUTED, font=("Segoe UI", 9), anchor="w", justify="left", wraplength=820)
        self.banner_detail.pack(fill="x", pady=(3, 0))
        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(10, 0), ipady=2)
        controls = tk.Frame(body, bg=BG)
        controls.pack(fill="x", pady=(8, 0))
        self.activity_button = ttk.Button(controls, text="Activity  +", style="Subtle.TButton", command=self._toggle_activity)
        self.activity_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Cancel", style="Danger.TButton", command=self._stop_worker, state="disabled")
        self.stop_button.pack(side="right")
        ttk.Button(controls, text="Copy URL", style="Subtle.TButton", command=self._copy_api_endpoint).pack(side="right", padx=(6, 0))
        ttk.Button(controls, text="Local API", style="Subtle.TButton", command=self._start_bridge).pack(side="right", padx=(6, 0))
        ttk.Button(controls, text="Diagnostics", style="Subtle.TButton", command=lambda: open_path(self.data_dir / "logs")).pack(side="right", padx=(6, 0))
        ttk.Button(controls, text="Exports", style="Subtle.TButton", command=lambda: open_path(Path(self.output_path_var.get()))).pack(side="right", padx=(6, 0))

        self.activity_panel = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        log_inner = tk.Frame(self.activity_panel, bg=PANEL)
        log_inner.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_text = tk.Text(log_inner, height=6, bg=PANEL, fg=MUTED, insertbackground=TEXT, bd=0, relief="flat", font=("Consolas", 9), wrap="word", padx=6, pady=4)
        log_scroll = ttk.Scrollbar(log_inner, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.configure(state="disabled")
        return page

    def _build_collection_page(self) -> tk.Frame:
        page = tk.Frame(self.content, bg=BG)
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=30, pady=22)
        self._page_header(body, "Collection", "Search, sort, and export the latest accepted snapshot. Unknown Arena IDs stay visible for review.")
        tools = tk.Frame(body, bg=BG)
        tools.pack(fill="x", pady=(0, 12))
        self.search_var = tk.StringVar()
        search = ttk.Entry(tools, textvariable=self.search_var)
        search.pack(side="left", fill="x", expand=True, ipady=2)
        ttk.Label(tools, text="Search cards", style="Muted.TLabel").pack(side="left", padx=(0, 8), before=search)
        search.bind("<KeyRelease>", lambda _event: self._refresh_collection())
        ttk.Button(tools, text="Refresh", style="Subtle.TButton", command=self._refresh_collection).pack(side="left", padx=(8, 0))
        ttk.Button(tools, text="Export files", style="Accent.TButton", command=self._export_current).pack(side="left", padx=(8, 0))
        columns = ("qty", "arena", "name", "set", "collector", "source", "review")
        table = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        table.pack(fill="both", expand=True)
        table.grid_rowconfigure(0, weight=1)
        table.grid_columnconfigure(0, weight=1)
        self.collection_tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        self.collection_sort_column: Optional[str] = None
        self.collection_sort_reverse = False
        self.collection_column_labels: Dict[str, str] = {}
        for key, label, width in (
            ("qty", "Qty", 55), ("arena", "Arena ID", 90), ("name", "Card name", 330),
            ("set", "Set", 70), ("collector", "Collector", 85), ("source", "Source", 115), ("review", "Review", 90),
        ):
            self.collection_column_labels[key] = label
            self.collection_tree.heading(key, text=label, command=lambda column=key: self._sort_collection(column))
            self.collection_tree.column(key, width=width, minwidth=45 if key != "name" else 160, anchor="w" if key == "name" else "center", stretch=key in ("name", "source"))
        self.collection_tree.tag_configure("stripe", background="#182432")
        collection_y = ttk.Scrollbar(table, orient="vertical", command=self.collection_tree.yview)
        collection_x = ttk.Scrollbar(table, orient="horizontal", command=self.collection_tree.xview)
        self.collection_tree.configure(yscrollcommand=collection_y.set, xscrollcommand=collection_x.set)
        self.collection_tree.grid(row=0, column=0, sticky="nsew")
        collection_y.grid(row=0, column=1, sticky="ns")
        collection_x.grid(row=1, column=0, sticky="ew")
        self.collection_empty = tk.Frame(table, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        self.collection_empty_title = ttk.Label(self.collection_empty, text="Your collection will appear here", style="Card.TLabel", font=("Segoe UI", 13, "bold"))
        self.collection_empty_title.pack(padx=22, pady=(18, 4))
        self.collection_empty_detail = ttk.Label(self.collection_empty, text="Sync Arena or import a snapshot to start your archive.", style="CardMuted.TLabel")
        self.collection_empty_detail.pack(padx=22, pady=(0, 12))
        ttk.Button(self.collection_empty, text="Go to sync", style="Accent.TButton", command=lambda: self._show_page("scan")).pack(pady=(0, 18))
        self.collection_empty.place(relx=0.5, rely=0.5, anchor="center")
        return page

    def _build_history_page(self) -> tk.Frame:
        page = tk.Frame(self.content, bg=BG)
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=30, pady=22)
        self._page_header(body, "Snapshot history", "A reliable record of each accepted full collection snapshot.")
        columns = ("id", "captured", "source", "version", "unique", "copies", "snapshot")
        table = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        table.pack(fill="both", expand=True)
        table.grid_rowconfigure(0, weight=1)
        table.grid_columnconfigure(0, weight=1)
        self.snapshot_tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        for key, label, width in (
            ("id", "Row", 55), ("captured", "Captured", 170), ("source", "Source", 120),
            ("version", "MTGA version", 145), ("unique", "IDs", 70), ("copies", "Copies", 75), ("snapshot", "Snapshot ID", 200),
        ):
            self.snapshot_tree.heading(key, text=label)
            self.snapshot_tree.column(key, width=width, minwidth=50, anchor="center", stretch=key in ("captured", "snapshot"))
        self.snapshot_tree.tag_configure("stripe", background="#182432")
        history_y = ttk.Scrollbar(table, orient="vertical", command=self.snapshot_tree.yview)
        history_x = ttk.Scrollbar(table, orient="horizontal", command=self.snapshot_tree.xview)
        self.snapshot_tree.configure(yscrollcommand=history_y.set, xscrollcommand=history_x.set)
        self.snapshot_tree.grid(row=0, column=0, sticky="nsew")
        history_y.grid(row=0, column=1, sticky="ns")
        history_x.grid(row=1, column=0, sticky="ew")
        self.history_empty = tk.Frame(table, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        self.history_empty_title = ttk.Label(self.history_empty, text="No snapshots yet", style="Card.TLabel", font=("Segoe UI", 13, "bold"))
        self.history_empty_title.pack(padx=24, pady=(18, 4))
        self.history_empty_detail = ttk.Label(self.history_empty, text="Accepted syncs and imports will be kept here.", style="CardMuted.TLabel")
        self.history_empty_detail.pack(padx=24, pady=(0, 18))
        self.history_empty.place(relx=0.5, rely=0.5, anchor="center")
        return page

    def _build_settings_page(self) -> tk.Frame:
        page = tk.Frame(self.content, bg=BG)
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=30, pady=22)
        self._page_header(body, "Settings", "Choose where Arena card data and exported snapshots are read and saved.")
        panel = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        panel.pack(fill="x")
        panel_inner = tk.Frame(panel, bg=PANEL)
        panel_inner.pack(fill="x", padx=20, pady=18)
        self.raw_path_var = tk.StringVar(value=str(self.settings.get("arena_raw_path", DEFAULT_RAW_FOLDER)))
        self.output_path_var = tk.StringVar(value=str(self.settings.get("output_path", app_output_dir())))
        self._path_setting(panel_inner, "Arena card database folder", self.raw_path_var, self._browse_raw)
        self._path_setting(panel_inner, "Export folder", self.output_path_var, self._browse_output)
        ttk.Label(
            panel_inner,
            text="Security: the Arena helper is downloaded unmodified from its public GitHub release, runs only on localhost on a random port, reads collection IDs and quantities, and is stopped after sync. Arena credentials are never collected or uploaded.",
            style="CardMuted.TLabel", wraplength=820, justify="left",
        ).pack(anchor="w", pady=(14, 12))
        self.settings_feedback = ttk.Label(panel_inner, text="", style="CardMuted.TLabel")
        self.settings_feedback.pack(anchor="w", pady=(0, 8))
        row = tk.Frame(panel_inner, bg=PANEL)
        row.pack(fill="x")
        ttk.Button(row, text="Open data folder", command=lambda: open_path(self.data_dir)).pack(side="left")
        ttk.Button(row, text="Save settings", style="Accent.TButton", command=self._save_settings).pack(side="right")
        return page

    def _path_setting(self, parent: ttk.Frame, label: str, variable: tk.StringVar, command: Any) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 5))
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", style="Subtle.TButton", command=command).pack(side="left", padx=(8, 0))

    def _resize_scan_copy(self, event: tk.Event) -> None:
        card_width = max(230, (int(event.width) - 48) // 2 - 44)
        self.auto_description.configure(wraplength=card_width)
        self.manual_description.configure(wraplength=card_width)
        self.banner_detail.configure(wraplength=max(320, int(event.width) - 205))

    def _bind_card_hover(self, panel: tk.Frame, idle_color: str, hover_color: str) -> None:
        def attach(widget: tk.Widget) -> None:
            widget.bind("<Enter>", lambda _event: panel.configure(highlightbackground=hover_color), add="+")
            widget.bind("<Leave>", lambda _event: panel.configure(highlightbackground=idle_color), add="+")
            for child in widget.winfo_children():
                attach(child)

        attach(panel)

    def _toggle_activity(self, open_state: Optional[bool] = None) -> None:
        self.activity_open = not self.activity_open if open_state is None else bool(open_state)
        if self.activity_open:
            self.activity_panel.pack(fill="x", pady=(8, 0))
            self.activity_button.configure(text="Activity  −")
        else:
            self.activity_panel.pack_forget()
            self.activity_button.configure(text="Activity  +")

    def _schedule_arena_presence(self) -> None:
        if self._closing:
            return

        def check() -> None:
            try:
                result = subprocess.run(
                    ["tasklist.exe", "/FI", "IMAGENAME eq MTGA.exe", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                    check=False,
                )
                running = any(line.strip().casefold().startswith('"mtga.exe"') for line in result.stdout.splitlines())
                self.events.put(("arena_presence", {"running": running}))
            except (OSError, subprocess.SubprocessError) as exc:
                self.events.put(("arena_presence", {"running": None, "error": str(exc)}))

        threading.Thread(target=check, name="arena-presence", daemon=True).start()
        self.after(5000, self._schedule_arena_presence)

    def _set_arena_presence(self, running: Optional[bool], error: str = "") -> None:
        self.arena_running = running
        if running is True:
            color, title, hint = GOOD, "ARENA RUNNING", "Stay on the Home screen before syncing"
            if self.banner_title.cget("text").startswith("READY"):
                self.banner_detail.configure(text="Arena detected. Wait for the Home screen, then sync your collection.")
        elif running is False:
            color, title, hint = WARN, "ARENA NOT OPEN", "Open MTG Arena to enable sync"
        else:
            color, title, hint = MUTED, "ARENA STATUS UNKNOWN", "Sync can still try the live connection"
            if error:
                self._append_log(time.strftime("%H:%M:%S"), "WARN", f"Could not check for Arena: {error}")
        self.arena_dot.itemconfigure(1, fill=color)
        self.arena_status_label.configure(text=title, fg=color)
        self.arena_status_hint.configure(text=hint)

    def _on_close(self) -> None:
        self._closing = True
        self.cancel_event.set()
        self.destroy()

    def _show_page(self, key: str) -> None:
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self.current_page = key
        page_titles = {
            "scan": "COLLECTION WORKSPACE",
            "collection": "COLLECTION  /  CARDS",
            "history": "COLLECTION  /  SNAPSHOTS",
            "settings": "PREFERENCES  /  STORAGE",
        }
        self.topbar_title.configure(text=page_titles.get(key, "COLLECTION WORKSPACE"))
        for name, button in self.nav_buttons.items():
            active = name == key
            button.configure(bg=PANEL if active else SIDEBAR, fg=TEXT if active else MUTED)
            self.nav_indicators[name].configure(bg=ACCENT if active else SIDEBAR)

    def _browse_manual(self) -> None:
        value = filedialog.askopenfilename(title="Choose collection JSON", filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if value:
            self.manual_path_var.set(value)

    def _browse_raw(self) -> None:
        value = filedialog.askdirectory(title="Choose MTGA_Data\\Downloads\\Raw")
        if value:
            self.raw_path_var.set(value)

    def _browse_output(self) -> None:
        value = filedialog.askdirectory(title="Choose export folder")
        if value:
            self.output_path_var.set(value)

    def _save_settings(self) -> None:
        self.settings.set("arena_raw_path", self.raw_path_var.get().strip())
        self.settings.set("output_path", self.output_path_var.get().strip())
        try:
            self.settings.save()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save settings:\n{exc}")
            return
        if hasattr(self, "settings_feedback"):
            self.settings_feedback.configure(text=f"Saved to {self.settings.path}", foreground=GOOD)
        if self.current_page == "scan":
            self._set_banner("SETTINGS SAVED", str(self.settings.path), "good")

    def _start_thread(self, target: Any, button_text: str) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_NAME, "Another operation is already running.")
            return
        self.cancel_event.clear()
        self.progress["value"] = 0
        self.memory_button.configure(text=button_text)
        self.memory_button.configure(state="disabled")
        self.manual_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._toggle_activity(True)
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _start_memory(self) -> None:
        self._save_settings()

        def worker() -> None:
            scan_id: Optional[int] = None
            try:
                started = time.monotonic()
                self.reporter.stage(1, 5, "Preparing one-click sync", "Checking the local read-only Arena helper.")
                provider = MtgaDaemonProvider(self.data_dir, self.reporter)
                status = provider.detect()
                if not status.available:
                    raise DaemonProviderError(status.message)
                self.reporter.log(status.message)

                self.reporter.stage(2, 5, "Reading Arena ownership", "No scrolling, screenshots, OCR, logs, or anchors are used.")
                snapshot = provider.read_collection()

                self.reporter.stage(3, 5, "Loading card identities", self.raw_path_var.get())
                database, database_source = prepare_database(
                    Path(self.raw_path_var.get()),
                    self.data_dir / "arena_cards.json",
                    self.reporter,
                    fallback_database=provider.identity_database,
                )
                observations, unresolved = collection_observations(
                    {card.arena_id: card.quantity for card in snapshot.cards}, database
                )

                self.reporter.stage(4, 5, "Validating complete snapshot", f"{snapshot.unique_cards:,} Arena IDs / {snapshot.total_copies:,} copies")
                scan_id = self.store.begin_session("mtga-memory", "One-click read-only Arena helper")
                details = {
                    "snapshotId": snapshot.snapshot_id,
                    "databaseSource": database_source,
                    "unresolvedArenaIds": unresolved,
                    "provider": snapshot.diagnostics,
                    "workflow": "one-click-daemon",
                }
                try:
                    summary, snapshot_row_id = self.store.apply_authoritative_snapshot(scan_id, snapshot, observations)
                except SuspiciousSnapshotError as exc:
                    self.pending_snapshot = (snapshot, observations, scan_id, details)
                    self.reporter.emit("confirm_snapshot", message=str(exc), stats=exc.to_dict())
                    return
                details["snapshotRowId"] = snapshot_row_id
                self._finish_snapshot(scan_id, snapshot, summary, details, started)
            except CancelledError:
                if scan_id is not None:
                    self.store.fail_session(scan_id, "Cancelled")
                self.reporter.emit("cancelled")
            except Exception as exc:
                if scan_id is not None:
                    self.store.fail_session(scan_id, str(exc))
                self.reporter.log(traceback.format_exc(), "DEBUG")
                self.reporter.emit("error", message=str(exc))

        self._start_thread(worker, "Syncing collection…")

    def _start_manual_import(self) -> None:
        path = Path(self.manual_path_var.get().strip())
        if not path.is_file():
            messagebox.showerror(APP_NAME, "Choose a JSON collection snapshot first.")
            return

        def worker() -> None:
            scan_id: Optional[int] = None
            try:
                started = time.monotonic()
                self.reporter.stage(1, 4, "Reading manual snapshot", str(path))
                snapshot = load_manual_snapshot(path)
                self.reporter.stage(2, 4, "Loading Arena card identities", self.raw_path_var.get())
                database, source = prepare_database(Path(self.raw_path_var.get()), self.data_dir / "arena_cards.json", self.reporter)
                observations, unresolved = collection_observations(
                    {card.arena_id: card.quantity for card in snapshot.cards}, database
                )
                scan_id = self.store.begin_session("manual-import", str(path))
                details = {
                    "snapshotId": snapshot.snapshot_id,
                    "sourceFile": str(path),
                    "databaseSource": source,
                    "unresolvedArenaIds": unresolved,
                }
                try:
                    summary, snapshot_row_id = self.store.apply_authoritative_snapshot(scan_id, snapshot, observations)
                except SuspiciousSnapshotError as exc:
                    self.pending_snapshot = (snapshot, observations, scan_id, details)
                    self.reporter.emit("confirm_snapshot", message=str(exc), stats=exc.to_dict())
                    return
                details["snapshotRowId"] = snapshot_row_id
                self._finish_snapshot(scan_id, snapshot, summary, details, started)
            except Exception as exc:
                if scan_id is not None:
                    self.store.fail_session(scan_id, str(exc))
                self.reporter.emit("error", message=str(exc))

        self._start_thread(worker, "Importing snapshot…")

    def _finish_snapshot(
        self,
        scan_id: int,
        snapshot: CollectionSnapshot,
        summary: MergeSummary,
        details: Dict[str, Any],
        started: float,
    ) -> None:
        self.reporter.stage(5, 5, "Writing exports", self.output_path_var.get())
        self.store.finish_session(summary, details=details)
        report = {
            "application": APP_NAME,
            "version": APP_VERSION,
            "mode": snapshot.source,
            "snapshot": snapshot.to_dict(),
            "merge": summary.to_dict(),
            **details,
        }
        paths = write_collection_exports(self.store.collection_rows(), Path(self.output_path_var.get()), report, self.reporter)
        canonical = Path(self.output_path_var.get()) / "mtga_collection_snapshot.json"
        canonical.write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
        self.reporter.emit(
            "complete",
            result={
                "snapshotId": snapshot.snapshot_id,
                "unique": snapshot.unique_cards,
                "copies": snapshot.total_copies,
                "source": snapshot.source,
                "paths": {key: str(value) for key, value in paths.items()},
                "durationSeconds": round(time.monotonic() - started, 2),
            },
        )

    def _confirm_pending_snapshot(self, payload: Dict[str, Any]) -> None:
        pending = self.pending_snapshot
        if pending is None:
            self._worker_done()
            return
        stats = payload.get("stats", {})
        answer = messagebox.askyesno(
            APP_NAME,
            f"{payload.get('message', 'Collection is substantially smaller.')}\n\n"
            f"Saved: {stats.get('current_unique', 0):,} IDs / {stats.get('current_total', 0):,} copies\n"
            f"New: {stats.get('new_unique', 0):,} IDs / {stats.get('new_total', 0):,} copies\n\n"
            "Replace the saved collection and retain both snapshots?",
        )
        snapshot, observations, scan_id, details = pending
        self.pending_snapshot = None
        if not answer:
            self.store.fail_session(scan_id, "User rejected suspicious collection drop")
            self._set_banner("SNAPSHOT NOT APPLIED", "The last valid collection was preserved.", "warn")
            self._worker_done()
            return

        def worker() -> None:
            try:
                started = time.monotonic()
                summary, snapshot_row_id = self.store.apply_authoritative_snapshot(
                    scan_id, snapshot, observations, allow_suspicious_drop=True
                )
                details["snapshotRowId"] = snapshot_row_id
                details["suspiciousDropConfirmed"] = True
                self._finish_snapshot(scan_id, snapshot, summary, details, started)
            except Exception as exc:
                self.store.fail_session(scan_id, str(exc))
                self.reporter.emit("error", message=str(exc))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _stop_worker(self) -> None:
        self.cancel_event.set()
        self._set_banner("CANCELLING", "The current read-only scan will stop at the next safe boundary.", "warn")
        self.stop_button.configure(state="disabled")

    def _start_bridge(self) -> None:
        if self.bridge_thread and self.bridge_thread.is_alive():
            self._set_banner("COLLECTION API RUNNING", "Listening only on 127.0.0.1:6842.", "good")
            return

        def serve() -> None:
            try:
                run_server(self.data_dir)
            except Exception as exc:
                self.reporter.emit("error", message=f"Local Collection API stopped: {exc}")

        self.bridge_thread = threading.Thread(target=serve, daemon=True)
        self.bridge_thread.start()
        self._set_banner("COLLECTION API STARTED", "Listening only on 127.0.0.1:6842. Token is in the Data folder.", "good")

    def _copy_api_endpoint(self) -> None:
        endpoint = "http://127.0.0.1:6842/v1/collection"
        self.clipboard_clear()
        self.clipboard_append(endpoint)
        self._set_banner("API ENDPOINT COPIED", endpoint + " — start the local API before using it.", "good")

    def _export_current(self) -> None:
        try:
            rows = self.store.collection_rows()
            if not rows:
                self._set_banner("NOTHING TO EXPORT", "Sync Arena or import a snapshot first.", "warn")
                return
            latest = self.store.latest_snapshot()
            report = {"application": APP_NAME, "version": APP_VERSION, "mode": "manual-export", "latestSnapshot": latest}
            paths = write_collection_exports(rows, Path(self.output_path_var.get()), report, self.reporter)
            self._set_banner("EXPORT COMPLETE", str(paths["json"]), "good")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _set_banner(self, title: str, detail: str, kind: str = "normal") -> None:
        colors = {"normal": ACCENT, "good": GOOD, "warn": WARN, "error": DANGER, "review": PURPLE}
        color = colors.get(kind, ACCENT)
        self.banner.configure(highlightbackground=(BORDER if kind == "normal" else color))
        self.banner_title.configure(text=title, fg=color)
        self.banner_detail.configure(text=detail)

    def _append_log(self, timestamp: str, level: str, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {level}: {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_events(self) -> None:
        try:
            while True:
                event_type, payload = self.events.get_nowait()
                if event_type == "log":
                    self._append_log(payload.get("timestamp", ""), payload.get("level", "INFO"), payload.get("message", ""))
                elif event_type == "status":
                    self._set_banner(payload.get("title", "WORKING"), payload.get("detail", ""), payload.get("kind", "normal"))
                elif event_type == "stage":
                    self._set_banner(payload.get("name", "WORKING").upper(), payload.get("detail", ""), "normal")
                elif event_type == "progress":
                    self.progress["value"] = float(payload.get("percent", 0.0))
                    if payload.get("label"):
                        self.banner_detail.configure(text=payload["label"])
                elif event_type == "confirm_snapshot":
                    self._confirm_pending_snapshot(payload)
                elif event_type == "complete":
                    result = payload.get("result", {})
                    self.progress["value"] = 100
                    self._set_banner(
                        "COLLECTION SYNCED",
                        f"{result.get('unique', 0):,} Arena IDs / {result.get('copies', 0):,} copies — {result.get('source', '')}",
                        "good",
                    )
                    self._worker_done()
                    self._refresh_all()
                elif event_type == "cancelled":
                    self._set_banner("CANCELLED", "The last valid snapshot was preserved.", "warn")
                    self._worker_done()
                elif event_type == "error":
                    self._set_banner("SYNC FAILED", payload.get("message", "Unknown error"), "error")
                    self._append_log(time.strftime("%H:%M:%S"), "ERROR", payload.get("message", "Unknown error"))
                    self._worker_done()
                elif event_type == "arena_presence":
                    self._set_arena_presence(payload.get("running"), payload.get("error", ""))
        except queue.Empty:
            pass
        if not self._closing:
            self.after(80, self._poll_events)

    def _worker_done(self) -> None:
        self.memory_button.configure(text="Sync my collection   →")
        self.memory_button.configure(state="normal")
        self.manual_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.worker = None

    def _refresh_all(self) -> None:
        self._refresh_metrics()
        self._refresh_collection()
        self._refresh_snapshots()

    def _refresh_metrics(self) -> None:
        values = self.store.metrics()
        for key, label in self.metric_labels.items():
            label.configure(text=f"{values.get(key, 0):,}")

    def _refresh_collection(self) -> None:
        if not hasattr(self, "collection_tree"):
            return
        for item in self.collection_tree.get_children():
            self.collection_tree.delete(item)
        search = self.search_var.get() if hasattr(self, "search_var") else ""
        rows = self.store.collection_rows(search=search)
        for index, row in enumerate(rows):
            review = "Review" if row.get("needs_review") or row.get("printing_uncertain") else ""
            self.collection_tree.insert(
                "", "end",
                values=(row["quantity"], row.get("arena_id", ""), row["name"], row["set_code"], row["collector_number"], row["source_method"], review),
                tags=(("stripe",) if index % 2 else ()),
            )
        if rows:
            self.collection_empty.place_forget()
        else:
            if self.store.metrics().get("unique_entries", 0):
                self.collection_empty_title.configure(text="No matching cards")
                self.collection_empty_detail.configure(text=f"No cards matched {search!r}. Clear the search to see your full collection.")
            else:
                self.collection_empty_title.configure(text="Your collection will appear here")
                self.collection_empty_detail.configure(text="Sync Arena or import a snapshot to start your archive.")
            self.collection_empty.place(relx=0.5, rely=0.5, anchor="center")
        if self.collection_sort_column:
            self._sort_collection(self.collection_sort_column, toggle=False)

    def _sort_collection(self, column: str, *, toggle: bool = True) -> None:
        if toggle:
            if self.collection_sort_column == column:
                self.collection_sort_reverse = not self.collection_sort_reverse
            else:
                self.collection_sort_column = column
                self.collection_sort_reverse = False
        if column not in self.collection_column_labels:
            return
        items = [(self.collection_tree.set(item, column), item) for item in self.collection_tree.get_children("")]

        def sort_key(item: tuple[str, str]) -> tuple[int, Any]:
            value = item[0]
            if column in ("qty", "arena"):
                try:
                    return (0, int(value))
                except (TypeError, ValueError):
                    pass
            return (1, value.casefold())

        items.sort(key=sort_key, reverse=self.collection_sort_reverse)
        for index, (_value, item) in enumerate(items):
            self.collection_tree.move(item, "", index)
            self.collection_tree.item(item, tags=(("stripe",) if index % 2 else ()))
        for key, label in self.collection_column_labels.items():
            arrow = "  ↑" if key == column and not self.collection_sort_reverse else ("  ↓" if key == column else "")
            self.collection_tree.heading(key, text=label + arrow)

    def _refresh_snapshots(self) -> None:
        if not hasattr(self, "snapshot_tree"):
            return
        for item in self.snapshot_tree.get_children():
            self.snapshot_tree.delete(item)
        rows = self.store.snapshot_rows()
        for index, row in enumerate(rows):
            self.snapshot_tree.insert(
                "", "end",
                values=(
                    row["snapshot_row_id"], row["captured_at"], row["source"], row.get("mtga_client_version") or "",
                    row["unique_cards"], row["total_copies"], row["snapshot_id"],
                ),
                tags=(("stripe",) if index % 2 else ()),
            )
        if rows:
            self.history_empty.place_forget()
        else:
            self.history_empty.place(relx=0.5, rely=0.5, anchor="center")


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
