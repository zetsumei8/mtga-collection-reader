"""Arena card-ID lookup loading from local MTGA data or Scryfall."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import requests

SCRYFALL_BULK_URL = "https://api.scryfall.com/bulk-data/default-cards"
REQUEST_HEADERS = {
    "User-Agent": "MTGA Collection Reader/5.2.0",
    "Accept": "application/json",
}


def _call(reporter: Any, method: str, *args: Any, **kwargs: Any) -> None:
    callback = getattr(reporter, method, None)
    if callable(callback):
        callback(*args, **kwargs)


def resolve_raw_path(selected: Path) -> Optional[Path]:
    try:
        base = selected.expanduser().resolve()
    except OSError:
        base = selected.expanduser()
    if base.is_file():
        base = base.parent
    candidates = [
        base,
        base / "Raw",
        base / "Downloads" / "Raw",
        base / "MTGA_Data" / "Downloads" / "Raw",
        base / "MTGA" / "MTGA_Data" / "Downloads" / "Raw",
    ]
    seen = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_dir() and candidate.name.casefold() == "raw":
            return candidate
    return None


def discover_mtga_raw_paths() -> Iterable[Path]:
    candidates = [
        Path(r"G:\MTGA\MTGA_Data\Downloads\Raw"),
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\MTGA\MTGA_Data\Downloads\Raw"),
        Path(r"C:\Program Files\Wizards of the Coast\MTGA\MTGA_Data\Downloads\Raw"),
        Path(r"C:\Program Files (x86)\Wizards of the Coast\MTGA\MTGA_Data\Downloads\Raw"),
    ]
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        value = os.environ.get(env_name)
        if value:
            candidates.append(Path(value) / "Wizards of the Coast" / "MTGA" / "MTGA_Data" / "Downloads" / "Raw")
    seen = set()
    for path in candidates:
        key = str(path).casefold()
        if key not in seen and path.is_dir():
            seen.add(key)
            yield path


def load_local_database(raw_path: Path, reporter: Any) -> Dict[int, Dict[str, str]]:
    raw_path = resolve_raw_path(raw_path) or raw_path
    _call(reporter, "status", "Reading Arena card database", str(raw_path))
    files = sorted(raw_path.glob("*.mtga"), key=lambda item: item.stat().st_size, reverse=True)
    lookup: Dict[int, Dict[str, str]] = {}
    for index, file_path in enumerate(files, start=1):
        _call(reporter, "progress", index - 1, max(1, len(files)), f"Checking {file_path.name}")
        if file_path.stat().st_size < 500 * 1024:
            continue
        connection = None
        try:
            connection = sqlite3.connect(f"file:{file_path}?mode=ro", uri=True)
            cursor = connection.cursor()
            tables = {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "Cards" not in tables or "Localizations" not in tables:
                continue
            localization: Dict[int, str] = {}
            try:
                cursor.execute("SELECT Id, Text FROM Localizations WHERE Format LIKE '%en-US%' OR Format IS NULL")
            except sqlite3.Error:
                cursor.execute("SELECT Id, Text FROM Localizations")
            for localization_id, text in cursor.fetchall():
                if text:
                    localization[int(localization_id)] = str(text)

            columns = [row[1] for row in cursor.execute("PRAGMA table_info(Cards)")]
            set_column = "ExpansionCode" if "ExpansionCode" in columns else "NULL"
            collector_column = "CollectorNumber" if "CollectorNumber" in columns else "NULL"
            cursor.execute(f"SELECT GrpId, TitleId, {set_column}, {collector_column} FROM Cards")
            for group_id, title_id, set_code, collector_number in cursor.fetchall():
                name = localization.get(int(title_id))
                if not name:
                    continue
                lookup[int(group_id)] = {
                    "name": name,
                    "set": str(set_code or "").upper(),
                    "collector_number": str(collector_number or ""),
                }
            if len(lookup) > 1000:
                _call(reporter, "log", f"Loaded {len(lookup):,} Arena cards from {file_path.name}.")
                return lookup
        except sqlite3.Error as exc:
            _call(reporter, "log", f"Skipped {file_path.name}: {exc}", "WARN")
        finally:
            if connection is not None:
                connection.close()
    return lookup


def fetch_scryfall_database(reporter: Any) -> Dict[int, Dict[str, str]]:
    _call(reporter, "status", "Downloading card database", "Local Arena card data was unavailable")
    metadata_response = requests.get(SCRYFALL_BULK_URL, headers=REQUEST_HEADERS, timeout=30)
    metadata_response.raise_for_status()
    download_uri = metadata_response.json().get("download_uri")
    if not download_uri:
        raise RuntimeError("Scryfall did not provide a bulk-data download URL.")

    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(prefix="mtga_cards_", suffix=".json", delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            with requests.get(download_uri, headers=REQUEST_HEADERS, stream=True, timeout=(30, 180)) as response:
                response.raise_for_status()
                total = int(response.headers.get("Content-Length", "0") or 0)
                downloaded = 0
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    temp_file.write(chunk)
                    downloaded += len(chunk)
                    _call(reporter, "progress", downloaded, total or max(1, downloaded), "Downloading card definitions")
        cards = json.loads(temp_path.read_text(encoding="utf-8"))
        lookup: Dict[int, Dict[str, str]] = {}
        for card in cards:
            arena_id = card.get("arena_id")
            if arena_id:
                lookup[int(arena_id)] = {
                    "name": str(card.get("name", "Unknown")),
                    "set": str(card.get("set", "")).upper(),
                    "collector_number": str(card.get("collector_number", "")),
                }
        _call(reporter, "log", f"Built a {len(lookup):,}-card Arena ID index from Scryfall.")
        return lookup
    finally:
        if temp_path:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def load_cached_database(cache_file: Path) -> Dict[int, Dict[str, str]]:
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    return {int(card_id): dict(info) for card_id, info in raw.items() if isinstance(info, dict)}


def save_cached_database(cache_file: Path, database: Dict[int, Dict[str, str]]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    temp = cache_file.with_suffix(".tmp")
    temp.write_text(json.dumps({str(key): value for key, value in database.items()}), encoding="utf-8")
    temp.replace(cache_file)


def prepare_database(
    raw_path: Optional[Path],
    cache_file: Path,
    reporter: Any,
    *,
    force_refresh: bool = False,
    fallback_database: Optional[Dict[int, Dict[str, str]]] = None,
) -> Tuple[Dict[int, Dict[str, str]], str]:
    if cache_file.is_file() and not force_refresh:
        try:
            database = load_cached_database(cache_file)
            if database:
                _call(reporter, "log", f"Loaded {len(database):,} cached Arena cards.")
                return database, "cache"
        except (OSError, ValueError, TypeError) as exc:
            _call(reporter, "log", f"Card cache could not be used: {exc}", "WARN")

    selected = resolve_raw_path(raw_path) if raw_path else None
    if selected is None:
        selected = next(iter(discover_mtga_raw_paths()), None)
    database: Dict[int, Dict[str, str]] = {}
    source = ""
    if selected and selected.is_dir():
        database = load_local_database(selected, reporter)
        source = "local Arena files"
    if not database and fallback_database:
        database = {int(key): dict(value) for key, value in fallback_database.items()}
        source = "Arena helper card database"
        _call(reporter, "log", f"Using {len(database):,} card identities returned by the Arena helper.")
    if not database:
        database = fetch_scryfall_database(reporter)
        source = "Scryfall"
    if not database:
        raise RuntimeError("No Arena card database could be loaded.")
    save_cached_database(cache_file, database)
    return database, source


def collection_observations(collection: Dict[int, int], database: Dict[int, Dict[str, str]]) -> Tuple[list[dict[str, Any]], int]:
    observations: list[dict[str, Any]] = []
    unresolved = 0
    for arena_id, quantity in collection.items():
        info = database.get(int(arena_id))
        if info is None:
            unresolved += 1
            observations.append(
                {
                    "arena_id": int(arena_id),
                    "name": f"Unknown Arena card {arena_id}",
                    "quantity": int(quantity),
                    "confidence": 0.0,
                    "needs_review": True,
                    "review_reason": "Arena card ID was not found in the local or cached card database",
                }
            )
            continue
        observations.append(
            {
                "arena_id": int(arena_id),
                "name": str(info.get("name", "Unknown")),
                "set_code": str(info.get("set", "")),
                "collector_number": str(info.get("collector_number", "")),
                "quantity": int(quantity),
                "confidence": 1.0,
                "needs_review": False,
            }
        )
    return observations, unresolved
