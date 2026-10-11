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
import socket
import stat
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import requests

from provider_contract import CollectionSnapshot, ProviderStatus, build_snapshot

UPSTREAM_REPOSITORY = "frcaton/mtga-tracker-daemon"
WINDOWS_ASSET_NAME = "mtga-tracker-daemon-Windows.zip"
PINNED_TAG = "1.0.11.0"
PINNED_DOWNLOAD_URL = (
    f"https://github.com/{UPSTREAM_REPOSITORY}/releases/download/"
    f"{PINNED_TAG}/{WINDOWS_ASSET_NAME}"
)
PINNED_ARCHIVE_SIZE = 33_956_616
PINNED_SHA256 = "b50a40cf87fe8ad0b51c1b23227b9e1f5d182e0416b6d841ba2c07bdb806117f"
PINNED_TREE_SHA256 = "5cdfd0a84f6d3cad6f2c6a42acbe3d482fb56f02d99a741a174f1e398a14a7c3"
PINNED_EXECUTABLE = Path("bin") / "mtga-tracker-daemon.exe"
PINNED_EXECUTABLE_SIZE = 152_064
PINNED_EXECUTABLE_SHA256 = "4d2093155ca5499662d808c1509c72a3a252ddfcf6ec1efd3a3b436a01a81e81"
USER_AGENT = "MTGA-Collection-Reader/5.2.5"
MAX_HELPER_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_HELPER_EXTRACTED_BYTES = 250 * 1024 * 1024


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


def _is_reparse_path(path: Path) -> bool:
    """Detect symlinks and Windows reparse points without following them."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    attributes = int(getattr(info, "st_file_attributes", 0))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _helper_tree_sha256(root: Path) -> str:
    """Hash all helper files and relative paths, rejecting links and special files."""
    root = Path(root)
    if _is_reparse_path(root) or not root.is_dir():
        raise DaemonProviderError("The installed Arena helper folder is missing or unsafe.")

    entries: list[tuple[bytes, bytes]] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories:
            if _is_reparse_path(current_path / name):
                raise DaemonProviderError("The installed Arena helper contains a linked folder.")
        for name in files:
            path = current_path / name
            if _is_reparse_path(path):
                raise DaemonProviderError("The installed Arena helper contains a linked file.")
            try:
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise DaemonProviderError("The installed Arena helper contains a non-regular file.")
                relative = path.relative_to(root).as_posix().encode("utf-8")
                entries.append((relative, bytes.fromhex(_sha256(path))))
            except OSError as exc:
                raise DaemonProviderError("The installed Arena helper could not be inspected safely.") from exc

    digest = hashlib.sha256()
    for relative, file_digest in sorted(entries):
        digest.update(relative + b"\0" + file_digest + b"\n")
    return digest.hexdigest()


def _verify_helper_directory(root: Path) -> Path:
    """Return the pinned executable only when the complete installed tree matches."""
    root = Path(root)
    executable = root / PINNED_EXECUTABLE
    if _helper_tree_sha256(root) != PINNED_TREE_SHA256:
        raise DaemonProviderError(
            "The installed Arena helper does not match the reviewed 1.0.11.0 release and will not be started."
        )
    try:
        info = executable.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != PINNED_EXECUTABLE_SIZE
            or _sha256(executable) != PINNED_EXECUTABLE_SHA256
        ):
            raise DaemonProviderError(
                "The installed Arena helper executable failed its integrity check and will not be started."
            )
    except OSError as exc:
        raise DaemonProviderError("The pinned Arena helper executable is missing or unreadable.") from exc
    return executable


def _install_root(data_dir: Path) -> Path:
    return Path(data_dir) / "tools" / "mtga-tracker-daemon" / PINNED_TAG


def _tools_root(data_dir: Path) -> Path:
    return Path(data_dir) / "tools" / "mtga-tracker-daemon"


def _ensure_tools_root(data_dir: Path) -> Path:
    tools_parent = Path(data_dir) / "tools"
    tools_root = _tools_root(data_dir)
    if _is_reparse_path(tools_parent) or _is_reparse_path(tools_root):
        raise DaemonProviderError("The Arena helper cache folder is linked or otherwise unsafe.")
    tools_root.mkdir(parents=True, exist_ok=True)
    if _is_reparse_path(tools_parent) or _is_reparse_path(tools_root) or not tools_root.is_dir():
        raise DaemonProviderError("The Arena helper cache folder is linked or otherwise unsafe.")
    return tools_root


def _write_install_marker(tools_root: Path, executable: Path, reporter: Any) -> None:
    marker_path = tools_root / "current.json"
    temp_path = tools_root / f".current-{os.getpid()}-{time.time_ns()}.tmp"
    try:
        marker = {
            "tag": PINNED_TAG,
            "executable": str(executable),
            "downloadUrl": PINNED_DOWNLOAD_URL,
            "sha256": PINNED_SHA256,
            "expectedSha256": PINNED_SHA256,
            "treeSha256": PINNED_TREE_SHA256,
            "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sourceRepository": f"https://github.com/{UPSTREAM_REPOSITORY}",
        }
        with temp_path.open("x", encoding="utf-8") as handle:
            json.dump(marker, handle, indent=2)
        temp_path.replace(marker_path)
    except OSError as exc:
        _call(reporter, "log", f"Could not refresh helper metadata: {exc}", "WARN")
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _quarantine_install(install_root: Path, reporter: Any) -> None:
    if not install_root.exists() and not install_root.is_symlink():
        return
    quarantine = install_root.with_name(
        f"{install_root.name}.rejected-{time.time_ns()}-{os.getpid()}"
    )
    try:
        install_root.rename(quarantine)
    except OSError as exc:
        raise DaemonProviderError(
            "The cached Arena helper failed verification and could not be preserved safely. "
            "It will not be started."
        ) from exc
    _call(reporter, "log", f"Preserved a modified helper cache at {quarantine}.", "WARN")


def verify_helper_for_launch(data_dir: Path, install: HelperInstall) -> HelperInstall:
    """Re-hash the expected pinned install immediately before process creation."""
    _ensure_tools_root(data_dir)
    executable = _verify_helper_directory(_install_root(data_dir))
    try:
        requested_path = install.executable.resolve(strict=True)
        expected_path = executable.resolve(strict=True)
    except OSError as exc:
        raise DaemonProviderError("The selected Arena helper path is missing or unreadable.") from exc
    if install.tag != PINNED_TAG or requested_path != expected_path:
        raise DaemonProviderError("The selected Arena helper is not the pinned release and will not be started.")
    return HelperInstall(PINNED_TAG, executable, PINNED_DOWNLOAD_URL, PINNED_SHA256)


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
        extracted_size = 0
        for info in zipped.infolist():
            relative = Path(info.filename.replace("\\", "/"))
            target = (destination_root / relative).resolve()
            is_symlink = (info.external_attr >> 16) & 0o170000 == 0o120000
            if relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(destination_root):
                raise DaemonProviderError("Downloaded helper archive contains an unsafe file path.")
            if is_symlink:
                raise DaemonProviderError("Downloaded helper archive contains an unsupported symbolic link.")
            extracted_size += info.file_size
            if extracted_size > MAX_HELPER_EXTRACTED_BYTES:
                raise DaemonProviderError("Downloaded helper archive is unexpectedly large.")
        zipped.extractall(destination_root)


def ensure_helper(data_dir: Path, reporter: Any = None) -> HelperInstall:
    if os.name != "nt":
        raise DaemonProviderError("Automatic MTGA collection sync requires Windows.")
    reporter = reporter or _NullReporter()
    tools_root = _ensure_tools_root(data_dir)
    install_root = _install_root(data_dir)
    try:
        executable = _verify_helper_directory(install_root)
    except DaemonProviderError:
        executable = None
    if executable is not None:
        _write_install_marker(tools_root, executable, reporter)
        _call(reporter, "log", f"Verified pinned Arena helper {PINNED_TAG} before use.")
        return HelperInstall(PINNED_TAG, executable, PINNED_DOWNLOAD_URL, PINNED_SHA256)

    _call(
        reporter,
        "status",
        "INSTALLING READ-ONLY ARENA HELPER",
        f"First sync only; downloading the pinned upstream {PINNED_TAG} release.",
    )
    _call(reporter, "log", f"Downloading pinned helper {PINNED_TAG} from {UPSTREAM_REPOSITORY}.")
    try:
        with tempfile.TemporaryDirectory(prefix=".helper-install-", dir=str(tools_root)) as temporary:
            staging_root = Path(temporary)
            archive = staging_root / WINDOWS_ASSET_NAME
            payload = staging_root / "payload"
            with requests.get(
                PINNED_DOWNLOAD_URL,
                headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
                timeout=(15, 180),
                stream=True,
            ) as response:
                response.raise_for_status()
                total = int(response.headers.get("Content-Length") or 0)
                if total and total != PINNED_ARCHIVE_SIZE:
                    raise DaemonProviderError("Pinned helper archive size did not match the reviewed release.")
                if total > MAX_HELPER_ARCHIVE_BYTES:
                    raise DaemonProviderError("Downloaded helper archive is unexpectedly large.")
                written = 0
                with archive.open("xb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 512):
                        _call(reporter, "check_cancelled")
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)
                        if written > MAX_HELPER_ARCHIVE_BYTES:
                            raise DaemonProviderError("Downloaded helper archive is unexpectedly large.")
                        _call(reporter, "progress", written, max(total, written), "Downloading Arena helper")
            if written != PINNED_ARCHIVE_SIZE:
                raise DaemonProviderError("Pinned helper archive size did not match the reviewed release.")
            archive_hash = _sha256(archive)
            if archive_hash.casefold() != PINNED_SHA256.casefold():
                raise DaemonProviderError(
                    "Downloaded helper archive did not match the fixed SHA-256 digest for 1.0.11.0. "
                    "The helper was not installed."
                )
            payload.mkdir()
            _extract_archive_safely(archive, payload)
            _verify_helper_directory(payload)

            if install_root.exists() or install_root.is_symlink():
                try:
                    existing_executable = _verify_helper_directory(install_root)
                except DaemonProviderError:
                    _quarantine_install(install_root, reporter)
                else:
                    _write_install_marker(tools_root, existing_executable, reporter)
                    return HelperInstall(PINNED_TAG, existing_executable, PINNED_DOWNLOAD_URL, PINNED_SHA256)
            try:
                payload.replace(install_root)
            except OSError as exc:
                # A second app instance may have installed the same pinned copy.
                try:
                    existing_executable = _verify_helper_directory(install_root)
                except DaemonProviderError:
                    raise DaemonProviderError("Could not install the verified Arena helper safely.") from exc
                _write_install_marker(tools_root, existing_executable, reporter)
                return HelperInstall(PINNED_TAG, existing_executable, PINNED_DOWNLOAD_URL, PINNED_SHA256)

        executable = _verify_helper_directory(install_root)
        _write_install_marker(tools_root, executable, reporter)
        _call(reporter, "log", f"Installed and verified Arena helper {PINNED_TAG}; SHA-256 {PINNED_SHA256}.")
        return HelperInstall(PINNED_TAG, executable, PINNED_DOWNLOAD_URL, PINNED_SHA256)
    except requests.RequestException as exc:
        raise DaemonProviderError(
            "Could not download the read-only Arena helper. Check internet access and retry, "
            "or use Manual JSON Import. " + str(exc)
        ) from exc
    except (OSError, zipfile.BadZipFile) as exc:
        raise DaemonProviderError(f"Could not install the Arena helper: {exc}") from exc


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
        self.install = verify_helper_for_launch(self.data_dir, self.install)
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
        install = ensure_helper(self.data_dir, self.reporter)
        self.install = install
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
