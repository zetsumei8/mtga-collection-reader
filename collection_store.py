"""Persistent collection storage and cross-session merge rules for MTGA Collection Reader."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_name(value: str) -> str:
    value = (value or "").strip().casefold()
    return re.sub(r"\s+", " ", value)


def stable_card_key(observation: Mapping[str, Any]) -> str:
    arena_id = observation.get("arena_id")
    if arena_id not in (None, ""):
        try:
            return f"arena:{int(arena_id)}"
        except (TypeError, ValueError):
            pass

    set_code = str(observation.get("set_code") or observation.get("set") or "").strip().upper()
    collector_number = str(
        observation.get("collector_number") or observation.get("cn") or ""
    ).strip()
    name = normalize_name(str(observation.get("name") or ""))
    if set_code and collector_number:
        return f"printing:{set_code}:{collector_number}"

    fingerprint = str(observation.get("fingerprint") or observation.get("tile_hash") or "").strip()
    if name and fingerprint:
        return f"visual:{name}:{fingerprint}"
    if name and set_code:
        return f"name-set:{name}:{set_code}"
    return f"name:{name or 'unknown'}"


@dataclass
class MergeSummary:
    scan_id: int
    source_method: str
    observed: int = 0
    new_entries: int = 0
    updated_entries: int = 0
    unchanged_entries: int = 0
    review_entries: int = 0
    ignored_entries: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SuspiciousSnapshotError(RuntimeError):
    """Raised before a full snapshot can replace a substantially larger saved collection."""

    def __init__(self, message: str, *, current_unique: int, current_total: int, new_unique: int, new_total: int) -> None:
        super().__init__(message)
        self.current_unique = current_unique
        self.current_total = current_total
        self.new_unique = new_unique
        self.new_total = new_total

    def to_dict(self) -> Dict[str, int]:
        return {
            "current_unique": self.current_unique,
            "current_total": self.current_total,
            "new_unique": self.new_unique,
            "new_total": self.new_total,
        }


class CollectionStore:
    """Thread-safe SQLite-backed collection database."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cards (
                    card_key TEXT PRIMARY KEY,
                    arena_id INTEGER,
                    name TEXT NOT NULL,
                    set_code TEXT NOT NULL DEFAULT '',
                    collector_number TEXT NOT NULL DEFAULT '',
                    quantity INTEGER NOT NULL CHECK(quantity >= 0),
                    confidence REAL NOT NULL DEFAULT 1.0,
                    needs_review INTEGER NOT NULL DEFAULT 0,
                    printing_uncertain INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_scan_id INTEGER,
                    source_method TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_cards_arena_id ON cards(arena_id);
                CREATE INDEX IF NOT EXISTS idx_cards_review ON cards(needs_review);

                CREATE TABLE IF NOT EXISTS scan_sessions (
                    scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    scan_type TEXT NOT NULL,
                    source_path TEXT NOT NULL DEFAULT '',
                    screens_accepted INTEGER NOT NULL DEFAULT 0,
                    screens_rejected INTEGER NOT NULL DEFAULT 0,
                    new_entries INTEGER NOT NULL DEFAULT 0,
                    updated_entries INTEGER NOT NULL DEFAULT 0,
                    unchanged_entries INTEGER NOT NULL DEFAULT 0,
                    review_entries INTEGER NOT NULL DEFAULT 0,
                    observed_entries INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'running',
                    details_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS scan_observations (
                    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id INTEGER NOT NULL,
                    card_key TEXT NOT NULL,
                    arena_id INTEGER,
                    name TEXT NOT NULL,
                    set_code TEXT NOT NULL DEFAULT '',
                    collector_number TEXT NOT NULL DEFAULT '',
                    observed_quantity INTEGER,
                    confidence REAL NOT NULL DEFAULT 0.0,
                    needs_review INTEGER NOT NULL DEFAULT 0,
                    screen_number INTEGER,
                    crop_path TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL DEFAULT '',
                    timestamp TEXT NOT NULL,
                    source_method TEXT NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(scan_id) REFERENCES scan_sessions(scan_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_observations_scan ON scan_observations(scan_id);
                CREATE INDEX IF NOT EXISTS idx_observations_key ON scan_observations(card_key);

                CREATE TABLE IF NOT EXISTS review_items (
                    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id INTEGER NOT NULL,
                    card_key TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    proposed_quantity INTEGER,
                    previous_quantity INTEGER,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(scan_id) REFERENCES scan_sessions(scan_id) ON DELETE CASCADE
                );


                CREATE TABLE IF NOT EXISTS collection_snapshots (
                    snapshot_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL UNIQUE,
                    scan_id INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    mtga_client_version TEXT,
                    captured_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL,
                    unique_cards INTEGER NOT NULL,
                    total_copies INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    diagnostics_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(scan_id) REFERENCES scan_sessions(scan_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_stored ON collection_snapshots(stored_at DESC);
                """
            )

    def _transaction(self):
        """Return a committing context manager that also closes its SQLite handle.

        ``sqlite3.Connection`` commits or rolls back when used as a context
        manager, but deliberately does not close itself. Explicitly closing is
        important on Windows, where a lingering handle prevents users from
        moving, backing up, or deleting the runtime folder.
        """
        connection = self._connect()

        class _Transaction:
            def __enter__(self_nonlocal) -> sqlite3.Connection:
                return connection.__enter__()

            def __exit__(self_nonlocal, exc_type: Any, exc: Any, tb: Any) -> None:
                try:
                    connection.__exit__(exc_type, exc, tb)
                finally:
                    connection.close()

        return _Transaction()

    def begin_session(self, scan_type: str, source_path: str = "") -> int:
        with self._lock, self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO scan_sessions(started_at, scan_type, source_path) VALUES (?, ?, ?)",
                (utc_now(), scan_type, source_path),
            )
            return int(cursor.lastrowid)

    def fail_session(self, scan_id: int, message: str) -> None:
        with self._lock, self._transaction() as connection:
            connection.execute(
                "UPDATE scan_sessions SET completed_at=?, status='error', details_json=? WHERE scan_id=?",
                (utc_now(), json.dumps({"error": message}), scan_id),
            )

    def finish_session(
        self,
        summary: MergeSummary,
        *,
        status: str = "complete",
        details: Optional[Mapping[str, Any]] = None,
        screens_accepted: int = 0,
        screens_rejected: int = 0,
    ) -> None:
        with self._lock, self._transaction() as connection:
            connection.execute(
                """
                UPDATE scan_sessions
                SET completed_at=?, status=?, screens_accepted=?, screens_rejected=?,
                    new_entries=?, updated_entries=?, unchanged_entries=?, review_entries=?,
                    observed_entries=?, details_json=?
                WHERE scan_id=?
                """,
                (
                    utc_now(),
                    status,
                    int(screens_accepted),
                    int(screens_rejected),
                    summary.new_entries,
                    summary.updated_entries,
                    summary.unchanged_entries,
                    summary.review_entries,
                    summary.observed,
                    json.dumps(dict(details or {}), default=str),
                    summary.scan_id,
                ),
            )

    def merge_observations(
        self,
        scan_id: int,
        observations: Iterable[Mapping[str, Any]],
        *,
        source_method: str,
    ) -> MergeSummary:
        summary = MergeSummary(scan_id=scan_id, source_method=source_method)
        timestamp = utc_now()

        with self._lock, self._transaction() as connection:
            for raw in observations:
                observation = dict(raw)
                summary.observed += 1
                name = str(observation.get("name") or "").strip()
                quantity_raw = observation.get("quantity", observation.get("count"))
                try:
                    quantity = int(quantity_raw) if quantity_raw is not None else None
                except (TypeError, ValueError):
                    quantity = None
                set_code = str(observation.get("set_code") or observation.get("set") or "").strip().upper()
                collector_number = str(
                    observation.get("collector_number") or observation.get("cn") or ""
                ).strip()
                arena_id: Optional[int]
                try:
                    arena_id = int(observation["arena_id"]) if observation.get("arena_id") not in (None, "") else None
                except (TypeError, ValueError):
                    arena_id = None
                confidence = float(observation.get("confidence", 1.0) or 0.0)
                fingerprint = str(observation.get("fingerprint") or observation.get("tile_hash") or "")
                screen_number = observation.get("screen_number", observation.get("page_number"))
                crop_path = str(observation.get("crop_path") or "")
                reason = str(observation.get("review_reason") or "").strip()
                requested_review = bool(observation.get("needs_review"))
                printing_uncertain = not arena_id and not (set_code and collector_number)

                key_data = {
                    "arena_id": arena_id,
                    "name": name,
                    "set_code": set_code,
                    "collector_number": collector_number,
                    "fingerprint": fingerprint,
                }
                card_key = stable_card_key(key_data)

                invalid = not name or quantity is None or quantity < 0
                needs_review = requested_review or invalid
                if invalid and not reason:
                    reason = "Missing or invalid card name/quantity"

                connection.execute(
                    """
                    INSERT INTO scan_observations(
                        scan_id, card_key, arena_id, name, set_code, collector_number,
                        observed_quantity, confidence, needs_review, screen_number, crop_path,
                        fingerprint, timestamp, source_method, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_id,
                        card_key,
                        arena_id,
                        name or "Unknown",
                        set_code,
                        collector_number,
                        quantity,
                        confidence,
                        int(needs_review),
                        int(screen_number) if screen_number not in (None, "") else None,
                        crop_path,
                        fingerprint,
                        timestamp,
                        source_method,
                        json.dumps(observation, default=str),
                    ),
                )

                existing = connection.execute(
                    "SELECT * FROM cards WHERE card_key=?", (card_key,)
                ).fetchone()
                if existing is None and arena_id is not None:
                    existing = connection.execute(
                        "SELECT * FROM cards WHERE arena_id=? ORDER BY last_seen_at DESC LIMIT 1",
                        (arena_id,),
                    ).fetchone()
                    if existing is not None:
                        card_key = str(existing["card_key"])

                if needs_review:
                    summary.review_entries += 1
                    previous_quantity = int(existing["quantity"]) if existing is not None else None
                    connection.execute(
                        """
                        INSERT INTO review_items(scan_id, card_key, reason, proposed_quantity,
                                                 previous_quantity, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            scan_id,
                            card_key,
                            reason or "Recognition requires review",
                            quantity,
                            previous_quantity,
                            timestamp,
                        ),
                    )
                    # Preserve a valid but unresolved observation in the persistent
                    # collection. This is especially important for a Player.log card
                    # ID that is newer than the local card database. Invalid reads
                    # without a usable name/quantity stay only in the review queue.
                    if not invalid:
                        if existing is None:
                            connection.execute(
                                """
                                INSERT INTO cards(
                                    card_key, arena_id, name, set_code, collector_number, quantity,
                                    confidence, needs_review, printing_uncertain, first_seen_at,
                                    last_seen_at, last_scan_id, source_method, metadata_json
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    card_key, arena_id, name, set_code, collector_number, quantity,
                                    confidence, int(printing_uncertain), timestamp, timestamp, scan_id,
                                    source_method, json.dumps({"fingerprint": fingerprint}, default=str),
                                ),
                            )
                        else:
                            saved_quantity = max(int(existing["quantity"]), int(quantity))
                            connection.execute(
                                """
                                UPDATE cards
                                SET quantity=?, last_seen_at=?, last_scan_id=?, needs_review=1,
                                    confidence=MAX(confidence, ?), source_method=?,
                                    name=CASE WHEN ?<>'' THEN ? ELSE name END,
                                    set_code=CASE WHEN ?<>'' THEN ? ELSE set_code END,
                                    collector_number=CASE WHEN ?<>'' THEN ? ELSE collector_number END
                                WHERE card_key=?
                                """,
                                (
                                    saved_quantity, timestamp, scan_id, confidence, source_method,
                                    name, name, set_code, set_code, collector_number, collector_number, card_key,
                                ),
                            )
                    continue

                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO cards(
                            card_key, arena_id, name, set_code, collector_number, quantity,
                            confidence, needs_review, printing_uncertain, first_seen_at,
                            last_seen_at, last_scan_id, source_method, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            card_key,
                            arena_id,
                            name,
                            set_code,
                            collector_number,
                            quantity,
                            confidence,
                            int(printing_uncertain),
                            timestamp,
                            timestamp,
                            scan_id,
                            source_method,
                            json.dumps({"fingerprint": fingerprint}, default=str),
                        ),
                    )
                    summary.new_entries += 1
                    continue

                previous_quantity = int(existing["quantity"])
                if quantity == previous_quantity:
                    connection.execute(
                        """
                        UPDATE cards
                        SET last_seen_at=?, last_scan_id=?, confidence=MAX(confidence, ?),
                            name=CASE WHEN ?<>'' THEN ? ELSE name END,
                            set_code=CASE WHEN ?<>'' THEN ? ELSE set_code END,
                            collector_number=CASE WHEN ?<>'' THEN ? ELSE collector_number END,
                            needs_review=0, printing_uncertain=?, source_method=?
                        WHERE card_key=?
                        """,
                        (
                            timestamp, scan_id, confidence, name, name, set_code, set_code,
                            collector_number, collector_number, int(printing_uncertain), source_method, card_key,
                        ),
                    )
                    summary.unchanged_entries += 1
                elif quantity > previous_quantity:
                    connection.execute(
                        """
                        UPDATE cards
                        SET quantity=?, last_seen_at=?, last_scan_id=?, confidence=?,
                            source_method=?, name=?, set_code=?, collector_number=?,
                            printing_uncertain=?, needs_review=0
                        WHERE card_key=?
                        """,
                        (
                            quantity,
                            timestamp,
                            scan_id,
                            confidence,
                            source_method,
                            name or existing["name"],
                            set_code or existing["set_code"],
                            collector_number or existing["collector_number"],
                            int(printing_uncertain and not (existing["set_code"] and existing["collector_number"])),
                            card_key,
                        ),
                    )
                    summary.updated_entries += 1
                else:
                    # Arena cards are not normally lost. A lower observation is retained
                    # for review instead of silently shrinking the saved collection.
                    connection.execute(
                        "UPDATE cards SET last_seen_at=?, last_scan_id=?, needs_review=1 WHERE card_key=?",
                        (timestamp, scan_id, card_key),
                    )
                    connection.execute(
                        """
                        INSERT INTO review_items(scan_id, card_key, reason, proposed_quantity,
                                                 previous_quantity, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            scan_id,
                            card_key,
                            "Observed quantity was lower than the saved collection",
                            quantity,
                            previous_quantity,
                            timestamp,
                        ),
                    )
                    summary.review_entries += 1

        return summary

    def apply_authoritative_snapshot(
        self,
        scan_id: int,
        snapshot: Any,
        observations: Iterable[Mapping[str, Any]],
        *,
        allow_suspicious_drop: bool = False,
    ) -> tuple[MergeSummary, int]:
        """Atomically replace collection state from a complete memory/manual snapshot.

        Empty snapshots are rejected by the provider contract. A large drop is
        blocked unless the caller obtains explicit user confirmation. Every
        accepted snapshot is retained before the live collection is replaced.
        """
        rows = [dict(item) for item in observations]
        if not rows:
            raise ValueError("An empty snapshot cannot replace the saved collection.")
        source_method = str(getattr(snapshot, "source", ""))
        if source_method not in {"mtga-memory", "manual-import"}:
            raise ValueError(f"Non-authoritative snapshot source: {source_method}")
        timestamp = utc_now()
        new_unique = len(rows)
        new_total = sum(int(row.get("quantity", row.get("count", 0)) or 0) for row in rows)

        with self._lock, self._transaction() as connection:
            existing_rows = connection.execute(
                "SELECT card_key, arena_id, quantity, first_seen_at FROM cards"
            ).fetchall()
            current_unique = len(existing_rows)
            current_total = sum(int(row["quantity"]) for row in existing_rows)
            if current_unique and not allow_suspicious_drop:
                unique_ratio = new_unique / max(1, current_unique)
                total_ratio = new_total / max(1, current_total)
                if unique_ratio < 0.65 or total_ratio < 0.65:
                    raise SuspiciousSnapshotError(
                        "The new collection is more than 35% smaller than the saved collection. "
                        "Confirm the replacement only if Arena really shows the smaller account/collection.",
                        current_unique=current_unique,
                        current_total=current_total,
                        new_unique=new_unique,
                        new_total=new_total,
                    )

            existing_by_key = {str(row["card_key"]): row for row in existing_rows}
            existing_by_arena = {
                int(row["arena_id"]): row
                for row in existing_rows
                if row["arena_id"] is not None
            }
            payload = snapshot.to_dict()
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO collection_snapshots(
                    snapshot_id, scan_id, schema_version, source, mtga_client_version,
                    captured_at, stored_at, unique_cards, total_copies, payload_json,
                    diagnostics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(snapshot.snapshot_id),
                    int(scan_id),
                    int(snapshot.schema_version),
                    source_method,
                    getattr(snapshot, "mtga_client_version", None),
                    str(snapshot.captured_at),
                    timestamp,
                    new_unique,
                    new_total,
                    json.dumps(payload, default=str),
                    json.dumps(dict(getattr(snapshot, "diagnostics", {}) or {}), default=str),
                ),
            )
            snapshot_row = connection.execute(
                "SELECT snapshot_row_id FROM collection_snapshots WHERE snapshot_id=?",
                (str(snapshot.snapshot_id),),
            ).fetchone()
            if snapshot_row is None:
                raise RuntimeError("The accepted snapshot could not be retained.")
            snapshot_row_id = int(snapshot_row[0])

            summary = MergeSummary(scan_id=scan_id, source_method=source_method)
            prepared: List[Dict[str, Any]] = []
            for raw in rows:
                observation = dict(raw)
                arena_id = int(observation["arena_id"])
                quantity = int(observation.get("quantity", observation.get("count", 0)))
                name = str(observation.get("name") or f"Unknown Arena card {arena_id}")
                set_code = str(observation.get("set_code") or observation.get("set") or "").upper()
                collector_number = str(observation.get("collector_number") or observation.get("cn") or "")
                confidence = float(observation.get("confidence", 1.0) or 0.0)
                needs_review = bool(observation.get("needs_review"))
                reason = str(observation.get("review_reason") or "")
                card_key = stable_card_key({"arena_id": arena_id})
                previous = existing_by_arena.get(arena_id) or existing_by_key.get(card_key)
                if previous is None:
                    summary.new_entries += 1
                    first_seen = timestamp
                elif int(previous["quantity"]) == quantity:
                    summary.unchanged_entries += 1
                    first_seen = str(previous["first_seen_at"])
                else:
                    summary.updated_entries += 1
                    first_seen = str(previous["first_seen_at"])
                if needs_review:
                    summary.review_entries += 1
                summary.observed += 1
                connection.execute(
                    """
                    INSERT INTO scan_observations(
                        scan_id, card_key, arena_id, name, set_code, collector_number,
                        observed_quantity, confidence, needs_review, screen_number, crop_path,
                        fingerprint, timestamp, source_method, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', '', ?, ?, ?)
                    """,
                    (
                        scan_id, card_key, arena_id, name, set_code, collector_number,
                        quantity, confidence, int(needs_review), timestamp, source_method,
                        json.dumps(observation, default=str),
                    ),
                )
                prepared.append({
                    "card_key": card_key,
                    "arena_id": arena_id,
                    "name": name,
                    "set_code": set_code,
                    "collector_number": collector_number,
                    "quantity": quantity,
                    "confidence": confidence,
                    "needs_review": int(needs_review),
                    "first_seen_at": first_seen,
                    "reason": reason,
                })

            observed_ids = {int(row["arena_id"]) for row in prepared}
            summary.ignored_entries = sum(
                1 for arena_id in existing_by_arena if arena_id not in observed_ids
            )
            connection.execute("DELETE FROM cards")
            for row in prepared:
                connection.execute(
                    """
                    INSERT INTO cards(
                        card_key, arena_id, name, set_code, collector_number, quantity,
                        confidence, needs_review, printing_uncertain, first_seen_at,
                        last_seen_at, last_scan_id, source_method, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["card_key"], row["arena_id"], row["name"], row["set_code"],
                        row["collector_number"], row["quantity"], row["confidence"],
                        row["needs_review"], row["first_seen_at"], timestamp, scan_id,
                        source_method, json.dumps({"snapshot_id": snapshot.snapshot_id}),
                    ),
                )
                if row["needs_review"]:
                    connection.execute(
                        """
                        INSERT INTO review_items(scan_id, card_key, reason, proposed_quantity,
                                                 previous_quantity, created_at)
                        VALUES (?, ?, ?, ?, NULL, ?)
                        """,
                        (
                            scan_id, row["card_key"], row["reason"] or "Arena card ID is not mapped",
                            row["quantity"], timestamp,
                        ),
                    )
            return summary, snapshot_row_id

    def latest_snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                """
                SELECT snapshot_row_id, snapshot_id, scan_id, schema_version, source,
                       mtga_client_version, captured_at, stored_at, unique_cards,
                       total_copies, payload_json, diagnostics_json
                FROM collection_snapshots ORDER BY snapshot_row_id DESC LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["payload"] = json.loads(result.pop("payload_json"))
            result["diagnostics"] = json.loads(result.pop("diagnostics_json"))
            return result

    def snapshot_rows(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_row_id, snapshot_id, scan_id, source, mtga_client_version,
                       captured_at, stored_at, unique_cards, total_copies
                FROM collection_snapshots ORDER BY snapshot_row_id DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    def collection_rows(self, search: str = "", review_only: bool = False) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if search.strip():
            clauses.append("(name LIKE ? OR set_code LIKE ? OR collector_number LIKE ? OR CAST(arena_id AS TEXT) LIKE ?)")
            term = f"%{search.strip()}%"
            params.extend([term, term, term, term])
        if review_only:
            clauses.append("needs_review=1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = (
            "SELECT card_key, arena_id, name, set_code, collector_number, quantity, confidence, "
            "needs_review, printing_uncertain, first_seen_at, last_seen_at, source_method "
            f"FROM cards{where} ORDER BY name COLLATE NOCASE, set_code, collector_number"
        )
        with self._lock, self._transaction() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def history_rows(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT scan_id, started_at, completed_at, scan_type, source_path,
                       new_entries, updated_entries, unchanged_entries, review_entries,
                       observed_entries, status
                FROM scan_sessions ORDER BY scan_id DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    def metrics(self) -> Dict[str, int]:
        with self._lock, self._transaction() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS unique_entries,
                       COALESCE(SUM(quantity), 0) AS owned_copies,
                       COALESCE(SUM(CASE WHEN needs_review=1 OR printing_uncertain=1 THEN 1 ELSE 0 END), 0) AS needs_review
                FROM cards
                """
            ).fetchone()
            open_reviews = connection.execute(
                "SELECT COUNT(*) FROM review_items WHERE status='open'"
            ).fetchone()[0]
            return {
                "unique_entries": int(row["unique_entries"]),
                "owned_copies": int(row["owned_copies"]),
                "needs_review": max(int(row["needs_review"]), int(open_reviews)),
            }

    def clear_collection(self) -> None:
        with self._lock, self._transaction() as connection:
            connection.executescript(
                "DELETE FROM review_items; DELETE FROM scan_observations; DELETE FROM cards; DELETE FROM scan_sessions;"
            )
