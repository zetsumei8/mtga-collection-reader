"""Low-friction MTG Arena collection provider using mtga-tracker-daemon.

The helper is an independent GPLv3 program downloaded unmodified from its
upstream GitHub release. This application starts it on a random loopback port,
reads /status and /cards, then shuts it down. The helper reads MTGA process
memory; this module never writes to MTGA memory or game files.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import requests

from provider_contract import CollectionSnapshot, ProviderStatus, build_snapshot

UPSTREAM_REPOSITORY = "frcaton/mtga-tracker-daemon"
LATEST_RELEASE_API = f"https://api.github.com/repos/{UPSTREAM_REPOSITORY}/releases/latest"
WINDOWS_ASSET_NAME = "mtga-tracker-daemon-Windows.zip"
PINNED_TAG = "1.0.11.0"
PINNED_DOWNLOAD_URL = (
    f"https://github.com/{UPSTREAM_REPOSITORY}/releases/download/"
    f"{PINNED_TAG}/{WINDOWS_ASSET_NAME}"
)
USER_AGENT = "MTGA-Collection-Reader/5.2.1"


class DaemonProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class HelperInstall:
    tag: str
    executable: Path
    download_url: str
    sha256: str


class _NullReporter:
    def log(self, _message: str, _level: str = "INFO") -> None:
        pass

    def status(self, _message: str, _detail: str = "") -> None:
        pass

    def progress(self, _current: int, _total: int, _label: str, _eta: Optional[float] = None) -> None:
        pass

    def check_cancelled(self) -> None:
        pass


def _call(reporter: Any, method: str, *args: Any, **kwargs: Any) -> None:
    callback = getattr(reporter, method, None)
    if callable(callback):
        callback(*args, **kwargs)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _release_asset(timeout: float = 12.0) -> tuple[str, str]:
    """Return (tag, download_url), falling back to the tested pinned release."""
    try:
        response = requests.get(
            LATEST_RELEASE_API,
            headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        tag = str(payload.get("tag_name") or PINNED_TAG).removeprefix("v")
        for asset in payload.get("assets") or []:
            if str(asset.get("name")) == WINDOWS_ASSET_NAME:
                url = str(asset.get("browser_download_url") or "")
                if url:
                    return tag, url
    except Exception:
        pass
    return PINNED_TAG, PINNED_DOWNLOAD_URL


def _find_executable(root: Path) -> Optional[Path]:
    direct = root / "bin" / "mtga-tracker-daemon.exe"
    if direct.is_file():
        return direct
    for candidate in root.rglob("mtga-tracker-daemon.exe"):
        if candidate.is_file():
            return candidate
    return None


def _extract_archive_safely(archive: Path, destination: Path) -> None:
    """Extract a helper release without allowing archive paths to escape staging.

    Release archives are treated as untrusted transport data even though they
    come from the upstream project. This blocks both ``../`` path traversal and
    ZIP entries that represent symbolic links.
    """
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive) as zipped:
        bad = zipped.testzip()
        if bad:
            raise DaemonProviderError(f"Downloaded helper archive is corrupt at {bad}.")
        for info in zipped.infolist():
            relative = Path(info.filename.replace("\\", "/"))
            target = (destination_root / relative).resolve()
            is_symlink = (info.external_attr >> 16) & 0o170000 == 0o120000
            if relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(destination_root):
                raise DaemonProviderError("Downloaded helper archive contains an unsafe file path.")
            if is_symlink:
                raise DaemonProviderError("Downloaded helper archive contains an unsupported symbolic link.")
        zipped.extractall(destination_root)


def ensure_helper(data_dir: Path, reporter: Any = None) -> HelperInstall:
    if os.name != "nt":
        raise DaemonProviderError("Automatic MTGA collection sync requires Windows.")
    reporter = reporter or _NullReporter()
    tools_root = data_dir / "tools" / "mtga-tracker-daemon"
    marker_path = tools_root / "current.json"

    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        executable = Path(str(marker.get("executable", "")))
        if executable.is_file():
            return HelperInstall(
                tag=str(marker.get("tag", "unknown")),
                executable=executable,
                download_url=str(marker.get("downloadUrl", "")),
                sha256=str(marker.get("sha256", "")),
            )
    except (OSError, ValueError, TypeError):
        pass

    tag, download_url = _release_asset()
    install_root = tools_root / tag
    existing = _find_executable(install_root)
    if existing:
        result = HelperInstall(tag, existing, download_url, "")
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {"tag": tag, "executable": str(existing), "downloadUrl": download_url, "sha256": ""},
                indent=2,
            ),
            encoding="utf-8",
        )
        return result

    _call(reporter, "status", "INSTALLING READ-ONLY ARENA HELPER", "First sync only; downloading the upstream Windows release.")
    _call(reporter, "log", f"Downloading unmodified helper {tag} from {UPSTREAM_REPOSITORY}.")
    tools_root.mkdir(parents=True, exist_ok=True)
    archive = tools_root / f"{WINDOWS_ASSET_NAME}.{os.getpid()}.tmp"
    staging = tools_root / f"staging-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        with requests.get(
            download_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
            timeout=(15, 180),
            stream=True,
        ) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0)
            written = 0
            with archive.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 512):
                    _call(reporter, "check_cancelled")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    _call(reporter, "progress", written, max(total, written), "Downloading Arena helper")
        archive_hash = _sha256(archive)
        staging.mkdir(parents=True, exist_ok=True)
        _extract_archive_safely(archive, staging)
        executable = _find_executable(staging)
        if executable is None:
            raise DaemonProviderError("The helper download did not contain mtga-tracker-daemon.exe.")
        shutil.rmtree(install_root, ignore_errors=True)
        staging.replace(install_root)
        executable = _find_executable(install_root)
        if executable is None:
            raise DaemonProviderError("The helper executable disappeared during installation.")
        result = HelperInstall(tag, executable, download_url, archive_hash)
        marker_path.write_text(
            json.dumps(
                {
                    "tag": tag,
                    "executable": str(executable),
                    "downloadUrl": download_url,
                    "sha256": archive_hash,
                    "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "sourceRepository": f"https://github.com/{UPSTREAM_REPOSITORY}",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        _call(reporter, "log", f"Installed Arena helper {tag}; SHA-256 {archive_hash}.")
        return result
    except requests.RequestException as exc:
        raise DaemonProviderError(
            "Could not download the read-only Arena helper. Check internet access and retry, "
            "or use Manual JSON Import. " + str(exc)
        ) from exc
    except (OSError, zipfile.BadZipFile) as exc:
        raise DaemonProviderError(f"Could not install the Arena helper: {exc}") from exc
    finally:
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


class DaemonProcess:
    def __init__(self, install: HelperInstall, data_dir: Path, reporter: Any = None) -> None:
        self.install = install
        self.data_dir = data_dir
        self.reporter = reporter or _NullReporter()
        self.port = _free_loopback_port()
        self.base_url = f"http://localhost:{self.port}"
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.log_handle: Any = None
        self.session = requests.Session()
        self.session.trust_env = False

    def start(self) -> Dict[str, Any]:
        log_dir = self.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "mtga-memory-helper.log"
        self.log_handle = log_path.open("ab")
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _call(self.reporter, "log", f"Starting read-only helper {self.install.tag} on localhost:{self.port}.")
        self.process = subprocess.Popen(
            [str(self.install.executable), "-p", str(self.port)],
            cwd=str(self.install.executable.parent),
            stdin=subprocess.DEVNULL,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        deadline = time.monotonic() + 25.0
        last_error = ""
        while time.monotonic() < deadline:
            _call(self.reporter, "check_cancelled")
            if self.process.poll() is not None:
                raise DaemonProviderError(
                    f"The Arena helper exited before it became ready. Diagnostics: {log_path}"
                )
            try:
                response = self.session.get(self.base_url + "/status", timeout=1.5)
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.25)
        raise DaemonProviderError(
            f"The Arena helper did not start in time ({last_error}). Diagnostics: {log_path}"
        )

    def get_json(self, endpoint: str, timeout: float = 60.0) -> Dict[str, Any]:
        response = self.session.get(self.base_url + endpoint, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise DaemonProviderError(f"Unexpected helper response from {endpoint}.")
        error = payload.get("error") or payload.get("Error")
        if error:
            raise DaemonProviderError(str(error))
        return payload

    def close(self) -> None:
        if self.process is not None:
            try:
                self.session.post(self.base_url + "/shutdown", timeout=2.0)
            except Exception:
                pass
            try:
                self.process.wait(timeout=4.0)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        if self.log_handle is not None:
            try:
                self.log_handle.close()
            except OSError:
                pass
        self.session.close()
        self.process = None

    def __enter__(self) -> "DaemonProcess":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


def _value(record: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in record:
            return record[name]
    lowered = {str(key).casefold(): value for key, value in record.items()}
    for name in names:
        if name.casefold() in lowered:
            return lowered[name.casefold()]
    return None


def parse_cards_payload(payload: Mapping[str, Any]) -> list[dict[str, int]]:
    raw_cards = _value(payload, "cards", "Cards")
    if not isinstance(raw_cards, list):
        raise DaemonProviderError("The Arena helper did not return a card list.")
    merged: Dict[int, int] = {}
    for item in raw_cards:
        if not isinstance(item, Mapping):
            continue
        try:
            arena_id = int(_value(item, "grpId", "GrpId", "arenaId", "ArenaId"))
            quantity = int(_value(item, "owned", "Owned", "quantity", "Quantity"))
        except (TypeError, ValueError):
            continue
        if arena_id > 0 and quantity > 0:
            merged[arena_id] = max(merged.get(arena_id, 0), quantity)
    if not merged:
        raise DaemonProviderError(
            "Arena returned an empty collection. Wait until Arena reaches the Home screen, then retry. "
            "The saved collection was not changed."
        )
    return [{"arenaId": arena_id, "quantity": quantity} for arena_id, quantity in sorted(merged.items())]


def parse_all_cards_payload(payload: Mapping[str, Any]) -> Dict[int, Dict[str, str]]:
    raw_cards = _value(payload, "cards", "Cards")
    if not isinstance(raw_cards, list):
        return {}
    database: Dict[int, Dict[str, str]] = {}
    for item in raw_cards:
        if not isinstance(item, Mapping):
            continue
        try:
            arena_id = int(_value(item, "grpId", "GrpId", "arenaId", "ArenaId"))
        except (TypeError, ValueError):
            continue
        name = str(_value(item, "title", "Title", "name", "Name") or "").strip()
        if arena_id <= 0 or not name:
            continue
        database[arena_id] = {
            "name": name,
            "set": str(_value(item, "expansionCode", "ExpansionCode", "set", "Set") or "").upper(),
            "collector_number": str(_value(item, "collectorNumber", "CollectorNumber") or ""),
        }
    return database


class MtgaDaemonProvider:
    """One-click collection provider backed by the upstream read-only daemon."""

    def __init__(self, data_dir: Path, reporter: Any = None) -> None:
        self.data_dir = data_dir
        self.reporter = reporter or _NullReporter()
        self.install: Optional[HelperInstall] = None
        self.identity_database: Dict[int, Dict[str, str]] = {}
        self.identity_error: str = ""

    def detect(self) -> ProviderStatus:
        if os.name != "nt":
            return ProviderStatus(False, "mtga-memory", "Automatic sync requires Windows.")
        try:
            self.install = ensure_helper(self.data_dir, self.reporter)
            return ProviderStatus(
                True,
                "mtga-memory",
                f"Read-only Arena helper {self.install.tag} is ready.",
                diagnostics={
                    "helperVersion": self.install.tag,
                    "helperExecutable": str(self.install.executable),
                    "helperSha256": self.install.sha256,
                    "sourceRepository": UPSTREAM_REPOSITORY,
                },
            )
        except Exception as exc:
            return ProviderStatus(False, "mtga-memory", str(exc))

    def read_collection(self) -> CollectionSnapshot:
        install = self.install or ensure_helper(self.data_dir, self.reporter)
        started = time.monotonic()
        _call(self.reporter, "status", "READING YOUR ARENA COLLECTION", "Arena remains untouched; the helper uses read-only process access.")
        with DaemonProcess(install, self.data_dir, self.reporter) as helper:
            status = helper.start()
            is_running = bool(_value(status, "isRunning", "IsRunning"))
            if not is_running:
                raise DaemonProviderError(
                    "MTG Arena is not ready. Open Arena, sign in, and wait for the Home screen, then click Get My Collection again."
                )
            pid = int(_value(status, "processId", "ProcessId") or -1)
            daemon_version = str(_value(status, "daemonVersion", "DaemonVersion") or install.tag)
            _call(self.reporter, "log", f"Attached read-only helper to MTGA process {pid}.")
            payload = helper.get_json("/cards", timeout=90.0)
            cards = parse_cards_payload(payload)
            helper_elapsed = _value(payload, "elapsedTime", "ElapsedTime")
            try:
                _call(self.reporter, "status", "READING ARENA CARD NAMES", "Using Arena's local card database; no screenshots or web lookup required.")
                all_cards_payload = helper.get_json("/allCards", timeout=120.0)
                self.identity_database = parse_all_cards_payload(all_cards_payload)
                if self.identity_database:
                    _call(self.reporter, "log", f"Loaded {len(self.identity_database):,} card identities through the Arena helper.")
            except Exception as exc:
                self.identity_error = str(exc)
                _call(self.reporter, "log", f"Arena helper card-name lookup was unavailable: {exc}", "WARN")
        return build_snapshot(
            source="mtga-memory",
            cards=cards,
            mtga_client_version=None,
            diagnostics={
                "provider": "mtga-tracker-daemon",
                "daemonVersion": daemon_version,
                "helperRelease": install.tag,
                "helperSha256": install.sha256,
                "processId": pid,
                "loopbackOnly": True,
                "helperElapsedMilliseconds": helper_elapsed,
                "elapsedSeconds": round(time.monotonic() - started, 2),
                "sourceRepository": UPSTREAM_REPOSITORY,
                "identityCount": len(self.identity_database),
                "identityError": self.identity_error or None,
            },
        )
