"""MTGA Collection Reader 5.2.5.

Normal use is one click: the app installs and starts an independent read-only
Arena helper, reads the complete owned-card snapshot, saves it, and stops the
helper. No screenshots, scrolling, OCR, Player.log parsing, or anchor cards are
required. Manual JSON remains the permanent fallback.
"""
from __future__ import annotations

import json
import math
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
        style.configure("Hero.TLabel", background="#111A20", foreground=TEXT)
        style.configure("HeroMuted.TLabel", background="#111A20", foreground="#A7B8B4")
        style.configure("Manual.TLabel", background=BG, foreground=TEXT)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 27, "bold"))
        style.configure("Metric.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 22, "bold"))
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
        style.configure(
            "TEntry", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT,
            padding=(9, 8), borderwidth=1, relief="flat", bordercolor=BORDER,
            lightcolor=BORDER, darkcolor=BORDER, focuscolor="#4C988A",
        )
        style.map(
            "TEntry",
            fieldbackground=[("focus", "#202F3E")],
            bordercolor=[("focus", "#4C988A"), ("!focus", BORDER)],
            lightcolor=[("focus", "#4C988A"), ("!focus", BORDER)],
            darkcolor=[("focus", "#4C988A"), ("!focus", BORDER)],
        )
        style.configure("TCombobox", fieldbackground=PANEL_2, foreground=TEXT)
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT)
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#17212A", bordercolor="#17212A", lightcolor=ACCENT, darkcolor=ACCENT, thickness=4)

    def _build_shell(self) -> None:
        sidebar = tk.Frame(self, bg=SIDEBAR, width=224)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=20, pady=(23, 25))
        mark = tk.Canvas(brand, width=40, height=40, bg=SIDEBAR, highlightthickness=0)
        mark.pack(side="left", padx=(0, 12))
        mark.create_oval(2, 2, 38, 38, fill="#14251F", outline="#35564B", width=1)
        mark.create_polygon(20, 7, 30, 20, 20, 33, 10, 20, fill=ACCENT, outline="")
        mark.create_oval(17, 17, 23, 23, fill="#E4FFF5", outline="")
        brand_copy = tk.Frame(brand, bg=SIDEBAR)
        brand_copy.pack(side="left", fill="y")
        tk.Label(brand_copy, text="ARENA", bg=SIDEBAR, fg=TEXT, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(brand_copy, text="COLLECTION READER", bg=SIDEBAR, fg="#82949D", font=("Segoe UI", 7, "bold")).pack(anchor="w", pady=(2, 0))
        tk.Frame(sidebar, bg="#202C34", height=1).pack(fill="x", padx=19)
        tk.Label(sidebar, text="LIBRARY", bg=SIDEBAR, fg="#667A83", font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=21, pady=(23, 9))
        self.nav_buttons: Dict[str, tk.Button] = {}
        self.nav_indicators: Dict[str, tk.Frame] = {}
        self.nav_rows: Dict[str, tk.Frame] = {}
        self.nav_icons: Dict[str, tk.Canvas] = {}
        for key, label in (("scan", "Sync collection"), ("collection", "Card library"), ("history", "Snapshots"), ("settings", "Settings")):
            row = tk.Frame(sidebar, bg=SIDEBAR, height=46)
            row.pack(fill="x", padx=10, pady=3)
            row.pack_propagate(False)
            indicator = tk.Frame(row, bg=SIDEBAR, width=3)
            indicator.pack(side="left", fill="y")
            icon = tk.Canvas(row, width=26, height=26, bg=SIDEBAR, highlightthickness=0)
            icon.pack(side="left", padx=(12, 6))
            self._paint_nav_icon(icon, key, MUTED)
            button = tk.Button(
                row, text=label, command=lambda page=key: self._show_page(page), bg=SIDEBAR, fg=MUTED,
                activebackground=PANEL_2, activeforeground=TEXT, bd=0, relief="flat", anchor="w", padx=4,
                pady=10, font=("Segoe UI", 9, "bold"), cursor="hand2", highlightthickness=0,
            )
            button.pack(side="left", fill="both", expand=True)
            button.bind("<Enter>", lambda _event, page=key: self._set_nav_hover(page, True))
            button.bind("<Leave>", lambda _event, page=key: self._set_nav_hover(page, False))
            self.nav_buttons[key] = button
            self.nav_indicators[key] = indicator
            self.nav_rows[key] = row
            self.nav_icons[key] = icon

        tk.Frame(sidebar, bg="#202C34", height=1).pack(fill="x", padx=19, pady=(18, 13))
        status_head = tk.Frame(sidebar, bg=SIDEBAR)
        status_head.pack(side="bottom", fill="x", padx=20, pady=(15, 4))
        self.arena_dot = tk.Canvas(status_head, width=12, height=12, bg=SIDEBAR, highlightthickness=0)
        self.arena_dot.pack(side="left", padx=(0, 8))
        self.arena_dot.create_oval(2, 2, 10, 10, fill=WARN, outline="")
        self.arena_status_label = tk.Label(status_head, text="CHECKING ARENA", bg=SIDEBAR, fg=MUTED, font=("Segoe UI", 8, "bold"), anchor="w")
        self.arena_status_label.pack(side="left", fill="x", expand=True)
        self.arena_status_hint = tk.Label(sidebar, text="Open Arena to enable sync", bg=SIDEBAR, fg="#7D8E97", font=("Segoe UI", 8), anchor="w", wraplength=176, justify="left")
        self.arena_status_hint.pack(side="bottom", fill="x", padx=20, pady=(0, 10))
        tk.Label(sidebar, text=f"VERSION {APP_VERSION}", bg=SIDEBAR, fg="#667A83", font=("Segoe UI", 8, "bold")).pack(side="bottom", anchor="w", padx=20, pady=(0, 15))

        self.content = tk.Frame(self, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)
        topbar = tk.Frame(self.content, bg=BG, height=62)
        topbar.pack(fill="x", padx=34, pady=(13, 0))
        topbar.pack_propagate(False)
        self.topbar_title = tk.Label(topbar, text="COLLECTION  /  SYNC", bg=BG, fg="#83969E", font=("Segoe UI", 8, "bold"), anchor="w")
        self.topbar_title.pack(side="left", fill="y")
        privacy = tk.Frame(topbar, bg="#111D1B", padx=10, pady=6)
        privacy.pack(side="right", anchor="center")
        tk.Canvas(privacy, width=10, height=10, bg="#111D1B", highlightthickness=0).pack(side="left", padx=(0, 6))
        privacy.winfo_children()[0].create_oval(2, 2, 8, 8, fill=ACCENT, outline="")
        tk.Label(privacy, text="ON THIS PC", bg="#111D1B", fg="#A3C7BC", font=("Segoe UI", 7, "bold")).pack(side="left")
        tk.Frame(self.content, bg="#202C34", height=1).pack(fill="x", padx=34)
        self.pages = {
            "scan": self._build_scan_page(),
            "collection": self._build_collection_page(),
            "history": self._build_history_page(),
            "settings": self._build_settings_page(),
        }

    def _paint_nav_icon(self, canvas: tk.Canvas, key: str, color: str) -> None:
        canvas.delete("all")
        if key == "scan":
            canvas.create_oval(5, 5, 21, 21, outline=color, width=1.5)
            canvas.create_arc(8, 8, 18, 18, start=35, extent=250, style="arc", outline=color, width=1.5)
            canvas.create_oval(12, 12, 14, 14, fill=color, outline="")
        elif key == "collection":
            for y, inset in ((5, 2), (10, 0), (15, 2)):
                canvas.create_rectangle(5 + inset, y, 21 - inset, y + 4, outline=color, width=1.2)
        elif key == "history":
            canvas.create_arc(5, 5, 21, 21, start=35, extent=290, style="arc", outline=color, width=1.5)
            canvas.create_polygon(19, 5, 22, 10, 16, 9, fill=color, outline="")
            canvas.create_line(13, 8, 13, 13, 17, 15, fill=color, width=1.4, capstyle="round")
        else:
            canvas.create_oval(7, 7, 19, 19, outline=color, width=1.5)
            canvas.create_oval(11, 11, 15, 15, fill=color, outline="")
            for angle in range(0, 360, 45):
                import math
                radians = angle * math.pi / 180
                canvas.create_line(13 + 7 * math.cos(radians), 13 + 7 * math.sin(radians), 13 + 10 * math.cos(radians), 13 + 10 * math.sin(radians), fill=color, width=1.4)

    def _set_nav_hover(self, key: str, hovered: bool) -> None:
        button = self.nav_buttons.get(key)
        if button is None:
            return
        if key == self.current_page:
            bg, fg, icon_color = "#15241F", TEXT, ACCENT
        elif hovered:
            bg, fg, icon_color = PANEL_2, TEXT, TEXT
        else:
            bg, fg, icon_color = SIDEBAR, MUTED, MUTED
        self.nav_rows[key].configure(bg=bg)
        button.configure(bg=bg, fg=fg)
        self.nav_indicators[key].configure(bg=ACCENT if key == self.current_page else bg)
        self.nav_icons[key].configure(bg=bg)
        self._paint_nav_icon(self.nav_icons[key], key, icon_color)

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
        body.pack(fill="both", expand=True, padx=34, pady=19)
        self._page_header(
            body,
            "Your collection, in view.",
            "Keep a private, searchable record of every card you own in MTG Arena.",
        )

        metrics = tk.Frame(body, bg=BG)
        metrics.pack(fill="x", pady=(1, 0))
        self.metric_labels: Dict[str, ttk.Label] = {}
        for index, (key, label) in enumerate((
            ("owned_copies", "OWNED COPIES"),
            ("unique_entries", "UNIQUE CARDS"),
            ("needs_review", "TO REVIEW"),
        )):
            stat = tk.Frame(metrics, bg=BG)
            stat.pack(side="left", fill="x", expand=True)
            inner = tk.Frame(stat, bg=BG)
            inner.pack(anchor="w", padx=(0, 15), pady=(0, 1))
            value = ttk.Label(inner, text="0", style="Metric.TLabel")
            value.pack(anchor="w", side="left", padx=(0, 9))
            ttk.Label(inner, text=label, style="Muted.TLabel", font=("Segoe UI", 8, "bold")).pack(anchor="w", side="left", pady=(6, 0))
            self.metric_labels[key] = value
            if index < 2:
                tk.Frame(metrics, bg="#26343A", width=1).pack(side="left", fill="y", padx=(0, 19), pady=6)

        source_row = tk.Frame(body, bg=BG)
        source_row.pack(fill="both", expand=True, pady=(10, 0))

        automatic = tk.Frame(source_row, bg="#111A20", highlightthickness=1, highlightbackground="#2B413D")
        automatic.pack(fill="both", expand=True)
        tk.Frame(automatic, bg="#61C8B1", height=2).pack(fill="x")
        hero_body = tk.Frame(automatic, bg="#111A20")
        hero_body.pack(fill="both", expand=True, padx=(25, 18), pady=(14, 13))
        hero_body.grid_columnconfigure(0, weight=1)
        hero_body.grid_columnconfigure(1, weight=0)
        hero_body.grid_rowconfigure(0, weight=1)
        auto_inner = tk.Frame(hero_body, bg="#111A20")
        auto_inner.grid(row=0, column=0, sticky="nsew")
        auto_inner.grid_columnconfigure(0, weight=1)
        auto_inner.grid_rowconfigure(0, weight=1)
        auto_content = tk.Frame(auto_inner, bg="#111A20")
        auto_content.grid(row=0, column=0, sticky="w")
        auto_top = tk.Frame(auto_content, bg="#111A20")
        auto_top.pack(fill="x")
        ttk.Label(auto_top, text="LIVE COLLECTION CAPTURE", style="HeroMuted.TLabel", font=("Segoe UI", 8, "bold")).pack(side="left")
        tk.Label(auto_top, text="LOCAL  ·  PRIVATE", bg="#1A2927", fg="#A8DCCB", padx=9, pady=4, font=("Segoe UI", 7, "bold")).pack(side="left", padx=(11, 0))
        ttk.Label(auto_content, text="Every card.\nOne private vault.", style="Hero.TLabel", font=("Segoe UI", 24, "bold"), justify="left").pack(anchor="w", pady=(12, 0))
        self.auto_description = ttk.Label(
            auto_content,
            text="Capture your complete Arena collection, then search or export it whenever you need it.",
            style="HeroMuted.TLabel", wraplength=480, justify="left", font=("Segoe UI", 9),
        )
        self.auto_description.pack(anchor="w", pady=(7, 12))
        step_row = tk.Frame(auto_content, bg="#111A20")
        step_row.pack(fill="x", pady=(0, 14))
        for index, (number, label) in enumerate((("01", "Open Arena"), ("02", "Sign in"), ("03", "Sync"))):
            step = tk.Frame(step_row, bg="#111A20")
            step.pack(side="left", padx=(0, 19))
            tk.Label(step, text=number, bg="#111A20", fg=ACCENT if index == 2 else "#8B9B9F", font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 7))
            tk.Label(step, text=label, bg="#111A20", fg="#C2CECE", font=("Segoe UI", 9)).pack(side="left")
        self.memory_button = ttk.Button(
            auto_content, text="Sync collection  →", style="Accent.TButton", command=self._start_memory
        )
        self.memory_button.pack(anchor="w")
        tk.Label(auto_content, text="Arena must be open, signed in, and at the Home screen.", bg="#111A20", fg="#8FA29D", font=("Segoe UI", 8)).pack(anchor="w", pady=(9, 0))

        self.sync_art = self._build_sync_art(hero_body)
        self.sync_art.grid(row=0, column=1, sticky="e", padx=(10, 0))
        self._bind_card_hover(automatic, "#2B413D", "#4E8172")

        manual = tk.Frame(source_row, bg=BG)
        manual.pack(fill="x", pady=(12, 0))
        file_mark = tk.Canvas(manual, width=28, height=28, bg=BG, highlightthickness=0)
        file_mark.pack(side="left", padx=(1, 10))
        file_mark.create_rectangle(7, 3, 21, 25, outline="#91A7A3", width=1.4)
        file_mark.create_line(10, 12, 18, 12, fill="#91A7A3")
        file_mark.create_line(10, 16, 18, 16, fill="#91A7A3")
        file_mark.create_line(10, 20, 16, 20, fill="#91A7A3")
        manual_inner = tk.Frame(manual, bg=BG)
        manual_inner.pack(side="left", fill="y", padx=(0, 15))
        ttk.Label(manual_inner, text="Import a saved snapshot", style="Manual.TLabel", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.manual_description = ttk.Label(
            manual_inner,
            text="JSON fallback", style="Muted.TLabel", font=("Segoe UI", 8), wraplength=220, justify="left",
        )
        self.manual_description.pack(anchor="w", pady=(2, 0))
        self.manual_path_var = tk.StringVar()
        self.manual_path_entry = ttk.Entry(manual, textvariable=self.manual_path_var)
        self.manual_path_entry.pack(side="left", fill="x", expand=True, ipady=3)
        ttk.Button(manual, text="Browse…", style="Subtle.TButton", command=self._browse_manual).pack(side="left", padx=(8, 0))
        self.manual_button = ttk.Button(manual, text="Import JSON", command=self._start_manual_import)
        self.manual_button.pack(side="left", padx=(7, 0))

        self.banner = tk.Frame(body, bg=BG)
        self.banner.pack(fill="x", pady=(9, 0))
        self.banner_mark = tk.Canvas(self.banner, width=10, height=10, bg=BG, highlightthickness=0)
        self.banner_mark.pack(side="left", padx=(2, 10))
        self.banner_mark.create_oval(1, 1, 9, 9, fill=ACCENT, outline="")
        banner_left = tk.Frame(self.banner, bg=BG)
        banner_left.pack(side="left", fill="x", expand=True)
        self.banner_title = tk.Label(banner_left, text="READY TO SYNC", bg=BG, fg=ACCENT, font=("Segoe UI", 9, "bold"), anchor="w")
        self.banner_title.pack(fill="x")
        self.banner_detail = tk.Label(banner_left, text="Your current collection stays safe until a complete snapshot is accepted.", bg=BG, fg=MUTED, font=("Segoe UI", 8), anchor="w", justify="left", wraplength=820)
        self.banner_detail.pack(fill="x", pady=(2, 0))
        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(8, 0), ipady=2)
        self.progress.pack_forget()
        controls = tk.Frame(body, bg=BG)
        controls.pack(fill="x", pady=(7, 0))
        self.controls = controls
        self.activity_button = ttk.Button(controls, text="Show activity  +", style="Subtle.TButton", command=self._toggle_activity)
        self.activity_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Cancel", style="Danger.TButton", command=self._stop_worker, state="disabled")
        self.tools_menu = tk.Menu(
            self, tearoff=False, bg=PANEL, fg=TEXT, activebackground="#21443D",
            activeforeground=TEXT, disabledforeground=MUTED, bd=0, relief="flat",
            font=("Segoe UI", 9), selectcolor=ACCENT,
        )
        self.tools_menu.add_command(label="Open exports folder", command=lambda: open_path(Path(self.output_path_var.get())))
        self.tools_menu.add_command(label="Open logs", command=lambda: open_path(self.data_dir / "logs"))
        self.tools_menu.add_separator()
        self.tools_menu.add_command(label="Start local API", command=self._start_bridge)
        self.tools_menu.add_command(label="Copy API address", command=self._copy_api_endpoint)
        self.tools_button = ttk.Button(controls, text="Tools  ▾", style="Subtle.TButton", command=self._show_tools_menu)
        self.tools_button.pack(side="right")
        ttk.Button(controls, text="Open exports", style="Subtle.TButton", command=lambda: open_path(Path(self.output_path_var.get()))).pack(side="right", padx=(0, 6))

        self.activity_panel = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        log_inner = tk.Frame(self.activity_panel, bg=PANEL)
        log_inner.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_text = tk.Text(log_inner, height=6, bg=PANEL, fg=MUTED, insertbackground=TEXT, bd=0, relief="flat", font=("Consolas", 9), wrap="word", padx=6, pady=4)
        log_scroll = ttk.Scrollbar(log_inner, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.configure(state="disabled")
        source_row.bind("<Configure>", self._resize_scan_copy, add="+")
        self._animate_sync_art()
        return page

    def _build_sync_art(self, parent: tk.Widget) -> tk.Canvas:
        art = tk.Canvas(parent, width=292, height=304, bg="#111A20", highlightthickness=0, bd=0)
        art.create_oval(28, 14, 264, 250, outline="#233A37", width=1)
        art.create_oval(39, 25, 253, 239, outline="#1B2D2B", width=1)
        art.create_arc(28, 14, 264, 250, start=28, extent=74, style="arc", outline="#4E8879", width=2, tags="orbit_arc")
        art.create_oval(26, 126, 34, 134, fill=ACCENT, outline="", tags="orbit_dot")
        try:
            bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
            self.sync_art_image = tk.PhotoImage(file=str(bundle_root / "assets" / "collection_vault.png"))
            art.create_image(146, 138, image=self.sync_art_image, tags="vault")
        except tk.TclError:
            art.create_text(146, 132, text="✦", fill=ACCENT, font=("Segoe UI", 54, "bold"), tags="vault")
        art.create_text(146, 284, text="PRIVATE COLLECTION", fill="#A6B9B2", font=("Segoe UI", 8, "bold"))
        art.create_text(146, 297, text="STORED ON THIS PC", fill="#738A83", font=("Segoe UI", 7, "bold"))
        return art

    def _animate_sync_art(self) -> None:
        if self._closing or not hasattr(self, "sync_art"):
            return
        phase = (time.monotonic() * 0.35) % (2 * math.pi)
        center_x, center_y = 146, 132
        radius_x, radius_y = 122, 120
        x = center_x + math.cos(phase) * radius_x
        y = center_y + math.sin(phase) * radius_y
        self.sync_art.coords("orbit_dot", x - 3, y - 3, x + 3, y + 3)
        color = GOOD if self.arena_running else ACCENT
        self.sync_art.itemconfigure("orbit_dot", fill=color)
        self.sync_art.itemconfigure("orbit_arc", start=int((phase * 57.2958) % 360))
        self.after(90, self._animate_sync_art)

    def _show_tools_menu(self) -> None:
        self.tools_menu.tk_popup(
            self.tools_button.winfo_rootx(),
            self.tools_button.winfo_rooty() + self.tools_button.winfo_height(),
        )

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
        card_width = max(300, int(event.width * 0.56) - 55)
        self.auto_description.configure(wraplength=card_width)
        self.manual_description.configure(wraplength=max(130, min(220, int(event.width * 0.26))))
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
            self.activity_button.configure(text="Hide activity  −")
        else:
            self.activity_panel.pack_forget()
            self.activity_button.configure(text="Show activity  +")

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
            "scan": "COLLECTION  /  SYNC",
            "collection": "COLLECTION  /  LIBRARY",
            "history": "COLLECTION  /  SNAPSHOTS",
            "settings": "PREFERENCES  /  STORAGE",
        }
        self.topbar_title.configure(text=page_titles.get(key, "COLLECTION WORKSPACE"))
        for name, button in self.nav_buttons.items():
            active = name == key
            bg = "#15241F" if active else SIDEBAR
            button.configure(bg=bg, fg=TEXT if active else MUTED)
            self.nav_rows[name].configure(bg=bg)
            self.nav_indicators[name].configure(bg=ACCENT if active else SIDEBAR)
            self.nav_icons[name].configure(bg=bg)
            self._paint_nav_icon(self.nav_icons[name], name, ACCENT if active else MUTED)

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
        self.progress.pack(fill="x", pady=(8, 0), ipady=2, before=self.controls)
        self.memory_button.configure(text=button_text)
        self.memory_button.configure(state="disabled")
        self.manual_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.stop_button.pack(side="right", padx=(6, 0))
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
        self.banner_mark.itemconfigure(1, fill=color)

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
        self.memory_button.configure(text="Sync collection  →")
        self.memory_button.configure(state="normal")
        self.manual_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.stop_button.pack_forget()
        if float(self.progress["value"]) < 100:
            self.progress.pack_forget()
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
