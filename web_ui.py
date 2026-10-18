"""CSS based desktop interface for MTGA Collection Reader.

The reader's Python workers, SQLite store, and import/export code remain the
source of truth. Qt WebEngine only presents the local HTML/CSS interface.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow


class LocalPage(QWebEnginePage):
    """Keep the embedded interface on its local, packaged resources."""

    def acceptNavigationRequest(self, url: QUrl, navigation_type: Any, is_main_frame: bool) -> bool:  # noqa: N802
        return url.scheme() in {"file", "qrc"}


class UiBridge(QObject):
    event = Signal(str)

    def __init__(self, window: "ReaderWindow") -> None:
        super().__init__(window)
        self.window = window

    @Slot(result="QVariant")
    def bootstrap(self) -> Dict[str, Any]:
        return self.window.bootstrap()

    @Slot(str, bool, int, int, str, bool, result="QVariant")
    def collectionPage(
        self, search: str, review_only: bool, offset: int, page_size: int,
        sort: str, reverse: bool,
    ) -> Dict[str, Any]:  # noqa: N802
        return self.window.collection_page(search, review_only, offset, page_size, sort, reverse)

    @Slot(result="QVariant")
    def snapshots(self) -> list[Dict[str, Any]]:
        return self.window.store.snapshot_rows(100)

    @Slot(result=str)
    def chooseSnapshot(self) -> str:  # noqa: N802
        value, _ = QFileDialog.getOpenFileName(
            self.window, "Choose collection snapshot", "", "JSON snapshots (*.json);;All files (*.*)"
        )
        return value

    @Slot(str, result=str)
    def browseRaw(self, current: str) -> str:  # noqa: N802
        return QFileDialog.getExistingDirectory(self.window, "Choose MTG Arena raw card data", current)

    @Slot(str, result=str)
    def browseExport(self, current: str) -> str:  # noqa: N802
        return QFileDialog.getExistingDirectory(self.window, "Choose export folder", current)

    @Slot(str, str, result=bool)
    def saveSettings(self, raw_path: str, output_path: str) -> bool:  # noqa: N802
        return self.window.save_settings(raw_path, output_path)

    @Slot()
    def startSync(self) -> None:  # noqa: N802
        self.window.start_sync()

    @Slot(str)
    def importSnapshot(self, path: str) -> None:  # noqa: N802
        self.window.import_snapshot(path)

    @Slot()
    def cancel(self) -> None:
        self.window.cancel()

    @Slot(bool)
    def resolveConfirmation(self, accepted: bool) -> None:  # noqa: N802
        self.window.resolve_confirmation(accepted)

    @Slot()
    def exportCurrent(self) -> None:  # noqa: N802
        self.window.export_current()

    @Slot()
    def openExports(self) -> None:  # noqa: N802
        self.window.open_path(self.window.output_path())

    @Slot()
    def openLogs(self) -> None:  # noqa: N802
        self.window.open_logs()

    @Slot()
    def openData(self) -> None:  # noqa: N802
        self.window.open_path(self.window.data_dir)

    @Slot()
    def startApi(self) -> None:  # noqa: N802
        self.window.start_api()

    @Slot()
    def copyApi(self) -> None:  # noqa: N802
        self.window.copy_api()

    @Slot()
    def checkArena(self) -> None:  # noqa: N802
        self.window.check_arena()


class ReaderWindow(QMainWindow):
    def __init__(self, core: Any) -> None:
        super().__init__()
        self.core = core
        self.setWindowTitle(f"{core.APP_NAME} {core.APP_VERSION}")
        self.resize(1240, 850)
        self.setMinimumSize(900, 650)

        self.data_dir: Path = core.app_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = core.Settings(self.data_dir / "settings.json")
        self.store = core.CollectionStore(self.data_dir / "collection.db")
        self.events: "queue.Queue[tuple[str, Dict[str, Any]]]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.reporter = core.Reporter(self.events, self.cancel_event)
        self.worker: Optional[threading.Thread] = None
        self.pending_snapshot: Optional[tuple[Any, list[dict[str, Any]], int, Dict[str, Any]]] = None
        self.bridge_thread: Optional[threading.Thread] = None
        self.arena_running: Optional[bool] = None
        self.arena_error = ""
        self.current_status: Dict[str, str] = {
            "title": "READY TO SYNC",
            "detail": "Your last saved collection is safe until a new snapshot is ready.",
            "kind": "ready",
        }
        self.closing = False

        self.view = QWebEngineView(self)
        self.view.setPage(LocalPage(self.view))
        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        channel = QWebChannel(self.view.page())
        self.bridge = UiBridge(self)
        channel.registerObject("backend", self.bridge)
        self.view.page().setWebChannel(channel)
        self.setCentralWidget(self.view)

        ui_dir = Path(__file__).resolve().parent / "ui"
        if getattr(sys, "frozen", False):
            ui_dir = Path(sys._MEIPASS) / "ui"
        self.view.setUrl(QUrl.fromLocalFile(str(ui_dir / "index.html")))

        self.event_timer = QTimer(self)
        self.event_timer.setInterval(80)
        self.event_timer.timeout.connect(self.poll_events)
        self.event_timer.start()
        self.arena_timer = QTimer(self)
        self.arena_timer.setInterval(5000)
        self.arena_timer.timeout.connect(self.check_arena)
        self.arena_timer.start()
        QTimer.singleShot(180, self.check_arena)

    def bootstrap(self) -> Dict[str, Any]:
        return {
            "version": self.core.APP_VERSION,
            "metrics": self.store.metrics(),
            "settings": {
                "arena_raw_path": str(self.settings.get("arena_raw_path", self.core.DEFAULT_RAW_FOLDER)),
                "output_path": str(self.settings.get("output_path", self.core.app_output_dir())),
            },
            "arena_running": self.arena_running,
            "arena_error": self.arena_error,
            "status": dict(self.current_status),
        }

    def collection_page(
        self, search: str, review_only: bool, offset: int, page_size: int,
        sort: str, reverse: bool,
    ) -> Dict[str, Any]:
        rows = self.store.collection_rows(search=search)
        if review_only:
            rows = [row for row in rows if row.get("needs_review") or row.get("printing_uncertain")]
        sort_fields = {
            "name": "name",
            "set_code": "set_code",
            "quantity": "quantity",
            "review": "needs_review",
        }
        field = sort_fields.get(sort, "name")
        if sort == "review":
            rows.sort(key=lambda row: bool(row.get("needs_review") or row.get("printing_uncertain")), reverse=reverse)
        elif sort == "quantity":
            rows.sort(key=lambda row: int(row.get("quantity", 0)), reverse=reverse)
        else:
            rows.sort(key=lambda row: str(row.get(field, "")).casefold(), reverse=reverse)
        total = len(rows)
        start = max(0, int(offset))
        limit = max(1, min(200, int(page_size)))
        return {
            "rows": rows[start : start + limit],
            "total": total,
            "totalCollection": self.store.metrics().get("unique_entries", 0),
        }

    def save_settings(self, raw_path: str, output_path: str) -> bool:
        raw = raw_path.strip()
        output = output_path.strip()
        if not raw or not output:
            self.emit("error", message="Choose both the Arena card data folder and export folder.")
            return False
        self.settings.set("arena_raw_path", raw)
        self.settings.set("output_path", output)
        try:
            self.settings.save()
        except OSError as exc:
            self.emit("error", message=f"Could not save settings: {exc}")
            return False
        self.emit("status", title="SETTINGS SAVED", detail=str(self.settings.path), kind="ready")
        return True

    def output_path(self) -> Path:
        return Path(str(self.settings.get("output_path", self.core.app_output_dir())))

    def emit(self, event_type: str, **payload: Any) -> None:
        if event_type == "status":
            self.current_status = {
                "title": str(payload.get("title", "READY TO SYNC")),
                "detail": str(payload.get("detail", "")),
                "kind": str(payload.get("kind", "ready")),
            }
        self.bridge.event.emit(json.dumps({"type": event_type, "payload": payload}, default=str))

    def check_arena(self) -> None:
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

    def _start_worker(self, target: Any) -> bool:
        if self.worker and self.worker.is_alive():
            self.emit("error", message="Another operation is already running.")
            return False
        self.cancel_event.clear()
        self.worker = threading.Thread(target=target, name="collection-reader-worker", daemon=True)
        self.worker.start()
        return True

    @Slot()
    def start_sync(self) -> None:
        self._start_worker(self._sync_worker)

    def _sync_worker(self) -> None:
        scan_id: Optional[int] = None
        try:
            started = time.monotonic()
            self.reporter.stage(1, 5, "Preparing one-click sync", "Checking the local read-only Arena helper.")
            provider = self.core.MtgaDaemonProvider(self.data_dir, self.reporter)
            status = provider.detect()
            if not status.available:
                raise self.core.DaemonProviderError(status.message)
            self.reporter.log(status.message)

            self.reporter.stage(2, 5, "Reading Arena ownership", "No scrolling, screenshots, OCR, logs, or anchors are used.")
            snapshot = provider.read_collection()
            raw_path = str(self.settings.get("arena_raw_path", self.core.DEFAULT_RAW_FOLDER))
            self.reporter.stage(3, 5, "Loading card identities", raw_path)
            database, database_source = self.core.prepare_database(
                Path(raw_path), self.data_dir / "arena_cards.json", self.reporter,
                fallback_database=provider.identity_database,
            )
            observations, unresolved = self.core.collection_observations(
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
            except self.core.SuspiciousSnapshotError as exc:
                self.pending_snapshot = (snapshot, observations, scan_id, details)
                self.events.put(("confirm_snapshot", {"message": str(exc), "stats": exc.to_dict()}))
                return
            details["snapshotRowId"] = snapshot_row_id
            self._finish_snapshot(scan_id, snapshot, summary, details, started)
        except self.core.CancelledError:
            if scan_id is not None:
                self.store.fail_session(scan_id, "Cancelled")
            self.events.put(("cancelled", {}))
        except Exception as exc:
            if scan_id is not None:
                self.store.fail_session(scan_id, str(exc))
            self.reporter.log(traceback.format_exc(), "DEBUG")
            self.events.put(("error", {"message": str(exc)}))

    @Slot(str)
    def import_snapshot(self, path_value: str) -> None:
        path = Path(path_value.strip())
        if not path.is_file():
            self.emit("error", message="Choose a JSON collection snapshot first.")
            return
        self._start_worker(lambda: self._import_worker(path))

    def _import_worker(self, path: Path) -> None:
        scan_id: Optional[int] = None
        try:
            started = time.monotonic()
            self.reporter.stage(1, 4, "Reading snapshot", str(path))
            snapshot = self.core.load_manual_snapshot(path)
            raw_path = str(self.settings.get("arena_raw_path", self.core.DEFAULT_RAW_FOLDER))
            self.reporter.stage(2, 4, "Loading Arena card identities", raw_path)
            database, source = self.core.prepare_database(Path(raw_path), self.data_dir / "arena_cards.json", self.reporter)
            observations, unresolved = self.core.collection_observations(
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
            except self.core.SuspiciousSnapshotError as exc:
                self.pending_snapshot = (snapshot, observations, scan_id, details)
                self.events.put(("confirm_snapshot", {"message": str(exc), "stats": exc.to_dict()}))
                return
            details["snapshotRowId"] = snapshot_row_id
            self._finish_snapshot(scan_id, snapshot, summary, details, started)
        except Exception as exc:
            if scan_id is not None:
                self.store.fail_session(scan_id, str(exc))
            self.events.put(("error", {"message": str(exc)}))

    def _finish_snapshot(self, scan_id: int, snapshot: Any, summary: Any, details: Dict[str, Any], started: float) -> None:
        output = self.output_path()
        self.reporter.stage(5, 5, "Writing exports", str(output))
        self.store.finish_session(summary, details=details)
        report = {
            "application": self.core.APP_NAME,
            "version": self.core.APP_VERSION,
            "mode": snapshot.source,
            "snapshot": snapshot.to_dict(),
            "merge": summary.to_dict(),
            **details,
        }
        paths = self.core.write_collection_exports(self.store.collection_rows(), output, report, self.reporter)
        output.mkdir(parents=True, exist_ok=True)
        (output / "mtga_collection_snapshot.json").write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
        self.events.put(("complete", {
            "result": {
                "snapshotId": snapshot.snapshot_id,
                "unique": snapshot.unique_cards,
                "copies": snapshot.total_copies,
                "source": snapshot.source,
                "paths": {key: str(value) for key, value in paths.items()},
                "durationSeconds": round(time.monotonic() - started, 2),
            }
        }))

    @Slot(bool)
    def resolve_confirmation(self, accepted: bool) -> None:
        pending = self.pending_snapshot
        self.pending_snapshot = None
        if pending is None:
            return
        snapshot, observations, scan_id, details = pending
        if not accepted:
            self.store.fail_session(scan_id, "User rejected suspicious collection drop")
            self.emit("snapshot_declined", message="The last valid collection was preserved.")
            return

        def apply_confirmed() -> None:
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
                self.events.put(("error", {"message": str(exc)}))

        self._start_worker(apply_confirmed)

    @Slot()
    def cancel(self) -> None:
        self.cancel_event.set()
        self.emit("status", title="CANCELLING", detail="The reader will stop at its next safe boundary.", kind="warn")

    @Slot()
    def export_current(self) -> None:
        def worker() -> None:
            try:
                rows = self.store.collection_rows()
                if not rows:
                    self.events.put(("export_empty", {}))
                    return
                latest = self.store.latest_snapshot()
                report = {
                    "application": self.core.APP_NAME,
                    "version": self.core.APP_VERSION,
                    "mode": "manual-export",
                    "latestSnapshot": latest,
                }
                paths = self.core.write_collection_exports(rows, self.output_path(), report, self.reporter)
                self.events.put(("export_complete", {"path": str(paths["json"])}))
            except Exception as exc:
                self.events.put(("error", {"message": str(exc)}))

        self._start_worker(worker)

    def open_path(self, path: Path, *, is_directory: bool = True) -> None:
        if is_directory:
            path.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.emit("error", message=f"Could not open {path}")

    def open_logs(self) -> None:
        log_path = self.data_dir / "logs" / "mtga-memory-helper.log"
        if log_path.is_file():
            self.open_path(log_path, is_directory=False)
        else:
            self.open_path(self.data_dir)
            self.emit("status", title="NO SYNC LOG YET", detail="Opened the data folder. Helper diagnostics appear after a live sync.", kind="warn")

    @Slot()
    def start_api(self) -> None:
        if self.bridge_thread and self.bridge_thread.is_alive():
            self.emit("api_status", message="The local API is already running at 127.0.0.1:6842.")
            return

        def serve() -> None:
            try:
                self.core.run_server(self.data_dir)
            except Exception as exc:
                self.events.put(("error", {"message": f"Local Collection API stopped: {exc}"}))

        self.bridge_thread = threading.Thread(target=serve, name="local-collection-api", daemon=True)
        self.bridge_thread.start()
        self.emit("api_status", message="Local API started at 127.0.0.1:6842. Its token is in the data folder.")

    @Slot()
    def copy_api(self) -> None:
        QApplication.clipboard().setText("http://127.0.0.1:6842/v1/collection")
        self.emit("api_status", message="API address copied. Start the local API before using it.")

    @Slot()
    def poll_events(self) -> None:
        try:
            while True:
                event_type, payload = self.events.get_nowait()
                if event_type == "arena_presence":
                    self.arena_running = payload.get("running")
                    self.arena_error = str(payload.get("error", ""))
                if event_type == "confirm_snapshot":
                    # The previous worker has returned by the time the confirmation is acted on.
                    self.worker = None
                if event_type in {"complete", "cancelled", "error", "export_complete", "export_empty"}:
                    self.worker = None
                if event_type == "complete":
                    payload["metrics"] = self.store.metrics()
                if event_type == "export_empty":
                    self.worker = None
                    self.events.put(("status", {"title": "NOTHING TO EXPORT", "detail": "Sync Arena or import a snapshot first.", "kind": "warn"}))
                if event_type == "status":
                    self.current_status = {
                        "title": str(payload.get("title", "READY TO SYNC")),
                        "detail": str(payload.get("detail", "")),
                        "kind": str(payload.get("kind", "ready")),
                    }
                self.bridge.event.emit(json.dumps({"type": event_type, "payload": payload}, default=str))
        except queue.Empty:
            pass

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        self.closing = True
        self.cancel_event.set()
        super().closeEvent(event)


def run(core: Any) -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(core.APP_NAME)
    app.setApplicationVersion(core.APP_VERSION)
    app.setStyle("Fusion")
    window = ReaderWindow(core)
    window.show()
    return app.exec()
