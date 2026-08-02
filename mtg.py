"""MTGA Collection Reader 5.2.3.

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
APP_VERSION = "5.2.3"
DEFAULT_RAW_FOLDER = Path(r"G:\MTGA\MTGA_Data\Downloads\Raw")

BG = "#10151D"
PANEL = "#171E28"
PANEL_2 = "#1D2632"
BORDER = "#2B3746"
TEXT = "#E8EDF3"
MUTED = "#98A6B7"
ACCENT = "#4C8DFF"
GOOD = "#2FB36F"
WARN = "#D5A83E"
DANGER = "#D85B5B"
PURPLE = "#9667D7"


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

        self._configure_style()
        self._build_shell()
        self._show_page("scan")
        self._refresh_all()
        self.after(80, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Segoe UI", 10), background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL, relief="flat")
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Card.TLabel", background=PANEL, foreground=TEXT)
        style.configure("CardMuted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 22, "bold"))
        style.configure("Metric.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI", 20, "bold"))
        style.configure("Accent.TButton", background=ACCENT, foreground="white", padding=(14, 8))
        style.map("Accent.TButton", background=[("active", "#6AA1FF"), ("disabled", BORDER)])
        style.configure("Danger.TButton", background=DANGER, foreground="white", padding=(12, 7))
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=25)
        style.configure("Treeview.Heading", background=PANEL_2, foreground=TEXT)
        style.map("Treeview", background=[("selected", BORDER)], foreground=[("selected", TEXT)])
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT)
        style.configure("TCombobox", fieldbackground=PANEL_2, foreground=TEXT)
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT)

    def _build_shell(self) -> None:
        sidebar = tk.Frame(self, bg="#0C1118", width=200)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="WB FOX", bg="#0C1118", fg=TEXT, font=("Segoe UI", 19, "bold")).pack(anchor="w", padx=22, pady=(24, 0))
        tk.Label(sidebar, text="COLLECTION BRIDGE", bg="#0C1118", fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=22, pady=(0, 28))
        self.nav_buttons: Dict[str, tk.Button] = {}
        for key, label in (("scan", "Sync"), ("collection", "Collection"), ("history", "Snapshots"), ("settings", "Settings")):
            button = tk.Button(
                sidebar, text=label, command=lambda page=key: self._show_page(page), bg="#0C1118", fg=MUTED,
                activebackground=PANEL, activeforeground=TEXT, bd=0, relief="flat", anchor="w", padx=22,
                pady=12, font=("Segoe UI", 10, "bold"), cursor="hand2", highlightthickness=0,
            )
            button.pack(fill="x", padx=8, pady=2)
            self.nav_buttons[key] = button
        tk.Label(sidebar, text=f"Version {APP_VERSION}", bg="#0C1118", fg="#647285", font=("Segoe UI", 8)).pack(side="bottom", anchor="w", padx=22, pady=18)

        self.content = tk.Frame(self, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)
        self.pages = {
            "scan": self._build_scan_page(),
            "collection": self._build_collection_page(),
            "history": self._build_history_page(),
            "settings": self._build_settings_page(),
        }

    def _page_header(self, parent: tk.Widget, title: str, subtitle: str) -> None:
        ttk.Label(parent, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(parent, text=subtitle, style="Muted.TLabel", wraplength=900).pack(anchor="w", pady=(2, 18))

    def _build_scan_page(self) -> tk.Frame:
        page = tk.Frame(self.content, bg=BG)
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=24)
        self._page_header(
            body,
            "Get my Arena collection",
            "Open MTG Arena, sign in, and wait for the Home screen. One click reads the complete collection from Arena using a temporary read-only local helper.",
        )

        metrics = tk.Frame(body, bg=BG)
        metrics.pack(fill="x")
        self.metric_labels: Dict[str, ttk.Label] = {}
        for key, label in (("owned_copies", "Owned copies"), ("unique_entries", "Arena IDs"), ("needs_review", "Mapping review")):
            card = ttk.Frame(metrics, style="Card.TFrame", padding=16)
            card.pack(side="left", fill="x", expand=True, padx=(0, 10 if key != "needs_review" else 0))
            value = ttk.Label(card, text="0", style="Metric.TLabel")
            value.pack(anchor="w")
            ttk.Label(card, text=label, style="CardMuted.TLabel").pack(anchor="w")
            self.metric_labels[key] = value

        source_row = tk.Frame(body, bg=BG)
        source_row.pack(fill="x", pady=(18, 0))

        automatic = ttk.Frame(source_row, style="Card.TFrame", padding=22)
        automatic.pack(side="left", fill="both", expand=True, padx=(0, 9))
        ttk.Label(automatic, text="Automatic Arena Sync", style="Card.TLabel", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            automatic,
            text=(
                "No collection scrolling. No screenshots. No OCR. No anchor cards. "
                "On the first run, the app downloads the unmodified open-source read-only helper; later syncs reuse it."
            ),
            style="CardMuted.TLabel", wraplength=500,
        ).pack(anchor="w", pady=(6, 12))
        steps = (
            "1. Open Arena and sign in.\n"
            "2. Wait for the Home screen.\n"
            "3. Click the button below."
        )
        ttk.Label(automatic, text=steps, style="Card.TLabel", wraplength=500).pack(anchor="w", pady=(0, 16))
        self.memory_button = ttk.Button(
            automatic, text="Get My MTGA Collection", style="Accent.TButton", command=self._start_memory
        )
        self.memory_button.pack(anchor="w")
        ttk.Label(
            automatic,
            text="The helper runs only on 127.0.0.1, reads the Arena process, returns grpId + quantity, and is stopped after the sync.",
            style="CardMuted.TLabel", wraplength=500,
        ).pack(anchor="w", pady=(12, 0))

        manual = ttk.Frame(source_row, style="Card.TFrame", padding=22)
        manual.pack(side="left", fill="both", expand=True, padx=(9, 0))
        ttk.Label(manual, text="Manual JSON Fallback", style="Card.TLabel", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            manual,
            text=(
                "Use a compatible snapshot, {grpId, owned} list, or Arena-ID/quantity map only when an Arena update temporarily breaks automatic sync. "
                "An empty or suspiciously smaller import cannot silently replace the saved collection."
            ),
            style="CardMuted.TLabel", wraplength=430,
        ).pack(anchor="w", pady=(6, 12))
        self.manual_path_var = tk.StringVar()
        row = tk.Frame(manual, bg=PANEL)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.manual_path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse", command=self._browse_manual).pack(side="left", padx=(8, 0))
        self.manual_button = ttk.Button(manual, text="Import JSON snapshot", command=self._start_manual_import)
        self.manual_button.pack(anchor="e", pady=(10, 0))

        self.banner = tk.Frame(body, bg=PANEL_2, highlightthickness=1, highlightbackground=BORDER)
        self.banner.pack(fill="x", pady=(18, 0))
        self.banner_title = tk.Label(self.banner, text="READY", bg=PANEL_2, fg=TEXT, font=("Segoe UI", 17, "bold"), anchor="w")
        self.banner_title.pack(fill="x", padx=18, pady=(13, 0))
        self.banner_detail = tk.Label(self.banner, text="Open Arena, wait for Home, then click Get My MTGA Collection.", bg=PANEL_2, fg=MUTED, font=("Segoe UI", 10), anchor="w")
        self.banner_detail.pack(fill="x", padx=18, pady=(2, 13))
        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(10, 0))
        controls = tk.Frame(body, bg=BG)
        controls.pack(fill="x", pady=(8, 0))
        self.stop_button = ttk.Button(controls, text="Cancel", style="Danger.TButton", command=self._stop_worker, state="disabled")
        self.stop_button.pack(side="right")
        ttk.Button(controls, text="Start local Collection API", command=self._start_bridge).pack(side="right", padx=(0, 8))
        ttk.Button(controls, text="Open exports", command=lambda: open_path(Path(self.output_path_var.get()))).pack(side="right", padx=(0, 8))

        diagnostics = ttk.Frame(body, style="Card.TFrame", padding=10)
        diagnostics.pack(fill="both", expand=True, pady=(12, 0))
        self.log_text = tk.Text(diagnostics, height=8, bg=PANEL, fg=MUTED, insertbackground=TEXT, bd=0, relief="flat", font=("Consolas", 9), wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        return page

    def _build_collection_page(self) -> tk.Frame:
        page = tk.Frame(self.content, bg=BG)
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=24)
        self._page_header(body, "Collection", "The latest accepted authoritative snapshot. Unknown Arena IDs are retained and marked for mapping review.")
        tools = tk.Frame(body, bg=BG)
        tools.pack(fill="x", pady=(0, 10))
        self.search_var = tk.StringVar()
        search = ttk.Entry(tools, textvariable=self.search_var)
        search.pack(side="left", fill="x", expand=True)
        search.bind("<KeyRelease>", lambda _event: self._refresh_collection())
        ttk.Button(tools, text="Refresh", command=self._refresh_collection).pack(side="left", padx=(8, 0))
        ttk.Button(tools, text="Export", command=self._export_current).pack(side="left", padx=(8, 0))
        columns = ("qty", "arena", "name", "set", "collector", "source", "review")
        self.collection_tree = ttk.Treeview(body, columns=columns, show="headings")
        for key, label, width in (
            ("qty", "Qty", 55), ("arena", "Arena ID", 90), ("name", "Card name", 330),
            ("set", "Set", 70), ("collector", "Collector", 85), ("source", "Source", 115), ("review", "Review", 90),
        ):
            self.collection_tree.heading(key, text=label)
            self.collection_tree.column(key, width=width, anchor="w" if key == "name" else "center")
        self.collection_tree.pack(fill="both", expand=True)
        return page

    def _build_history_page(self) -> tk.Frame:
        page = tk.Frame(self.content, bg=BG)
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=24)
        self._page_header(body, "Snapshot history", "Every accepted full collection snapshot is retained for diagnosis and rollback work.")
        columns = ("id", "captured", "source", "version", "unique", "copies", "snapshot")
        self.snapshot_tree = ttk.Treeview(body, columns=columns, show="headings")
        for key, label, width in (
            ("id", "Row", 55), ("captured", "Captured", 170), ("source", "Source", 120),
            ("version", "MTGA version", 145), ("unique", "IDs", 70), ("copies", "Copies", 75), ("snapshot", "Snapshot ID", 200),
        ):
            self.snapshot_tree.heading(key, text=label)
            self.snapshot_tree.column(key, width=width, anchor="center")
        self.snapshot_tree.pack(fill="both", expand=True)
        return page

    def _build_settings_page(self) -> tk.Frame:
        page = tk.Frame(self.content, bg=BG)
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=24)
        self._page_header(body, "Settings", "MTGA paths and local-only bridge storage.")
        panel = ttk.Frame(body, style="Card.TFrame", padding=18)
        panel.pack(fill="x")
        self.raw_path_var = tk.StringVar(value=str(self.settings.get("arena_raw_path", DEFAULT_RAW_FOLDER)))
        self.output_path_var = tk.StringVar(value=str(self.settings.get("output_path", app_output_dir())))
        self._path_setting(panel, "Arena card database folder", self.raw_path_var, self._browse_raw)
        self._path_setting(panel, "Export folder", self.output_path_var, self._browse_output)
        ttk.Label(
            panel,
            text="Security: the Arena helper is downloaded unmodified from its public GitHub release, runs only on localhost on a random port, reads collection IDs and quantities, and is stopped after sync. Arena credentials are never collected or uploaded.",
            style="CardMuted.TLabel", wraplength=820,
        ).pack(anchor="w", pady=(14, 8))
        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill="x")
        ttk.Button(row, text="Open data folder", command=lambda: open_path(self.data_dir)).pack(side="left")
        ttk.Button(row, text="Save settings", style="Accent.TButton", command=self._save_settings).pack(side="right")
        return page

    def _path_setting(self, parent: ttk.Frame, label: str, variable: tk.StringVar, command: Any) -> None:
        ttk.Label(parent, text=label, style="Card.TLabel", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(6, 3))
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse", command=command).pack(side="left", padx=(8, 0))

    def _show_page(self, key: str) -> None:
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        for name, button in self.nav_buttons.items():
            button.configure(bg=PANEL if name == key else "#0C1118", fg=TEXT if name == key else MUTED)

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
        self.settings.save()
        self._set_banner("SETTINGS SAVED", str(self.settings.path), "good")

    def _start_thread(self, target: Any) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_NAME, "Another operation is already running.")
            return
        self.cancel_event.clear()
        self.progress["value"] = 0
        self.memory_button.configure(state="disabled")
        self.manual_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
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

        self._start_thread(worker)

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

        self._start_thread(worker)

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
        canonical = Path(self.output_path_var.get()) / "wb_fox_collection_snapshot.json"
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

    def _export_current(self) -> None:
        try:
            latest = self.store.latest_snapshot()
            report = {"application": APP_NAME, "version": APP_VERSION, "mode": "manual-export", "latestSnapshot": latest}
            paths = write_collection_exports(self.store.collection_rows(), Path(self.output_path_var.get()), report, self.reporter)
            self._set_banner("EXPORT COMPLETE", str(paths["json"]), "good")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _set_banner(self, title: str, detail: str, kind: str = "normal") -> None:
        colors = {"normal": TEXT, "good": GOOD, "warn": WARN, "error": DANGER, "review": PURPLE}
        self.banner_title.configure(text=title, fg=colors.get(kind, TEXT))
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
        except queue.Empty:
            pass
        self.after(80, self._poll_events)

    def _worker_done(self) -> None:
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
        for row in self.store.collection_rows(search=search):
            review = "Review" if row.get("needs_review") or row.get("printing_uncertain") else ""
            self.collection_tree.insert(
                "", "end",
                values=(row["quantity"], row.get("arena_id", ""), row["name"], row["set_code"], row["collector_number"], row["source_method"], review),
            )

    def _refresh_snapshots(self) -> None:
        if not hasattr(self, "snapshot_tree"):
            return
        for item in self.snapshot_tree.get_children():
            self.snapshot_tree.delete(item)
        for row in self.store.snapshot_rows():
            self.snapshot_tree.insert(
                "", "end",
                values=(
                    row["snapshot_row_id"], row["captured_at"], row["source"], row.get("mtga_client_version") or "",
                    row["unique_cards"], row["total_copies"], row["snapshot_id"],
                ),
            )


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
