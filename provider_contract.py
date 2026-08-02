"""Collection provider contracts shared by the Reader and WB Fox Bridge."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence


SCHEMA_VERSION = 1
AUTHORITATIVE_SOURCES = {"mtga-memory", "manual-import"}
# Arena's card database is far below this. The ceiling prevents a malformed or
# hostile manual input from exhausting memory while retaining ample future headroom.
MAX_SNAPSHOT_CARDS = 100_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ProviderStatus:
    available: bool
    provider: str
    message: str
    mtga_pid: Optional[int] = None
    mtga_client_version: Optional[str] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CollectionCard:
    arena_id: int
    quantity: int

    def to_dict(self) -> Dict[str, int]:
        return {"arenaId": int(self.arena_id), "quantity": int(self.quantity)}


@dataclass(frozen=True)
class CollectionSnapshot:
    source: str
    cards: Sequence[CollectionCard]
    captured_at: str = field(default_factory=utc_now)
    mtga_client_version: Optional[str] = None
    schema_version: int = SCHEMA_VERSION
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    @property
    def unique_cards(self) -> int:
        return len(self.cards)

    @property
    def total_copies(self) -> int:
        return sum(int(card.quantity) for card in self.cards)

    @property
    def snapshot_id(self) -> str:
        canonical = json.dumps(
            {
                "schemaVersion": self.schema_version,
                "source": self.source,
                "mtgaClientVersion": self.mtga_client_version,
                "cards": [card.to_dict() for card in sorted(self.cards, key=lambda item: item.arena_id)],
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()[:24]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "snapshotId": self.snapshot_id,
            "source": self.source,
            "mtgaClientVersion": self.mtga_client_version,
            "capturedAt": self.captured_at,
            "cards": [card.to_dict() for card in sorted(self.cards, key=lambda item: item.arena_id)],
            "diagnostics": dict(self.diagnostics),
        }


class CollectionProvider(Protocol):
    def detect(self) -> ProviderStatus:
        ...

    def read_collection(self) -> CollectionSnapshot:
        ...


class SnapshotValidationError(ValueError):
    pass


def normalize_cards(raw_cards: Iterable[Mapping[str, Any]]) -> tuple[List[CollectionCard], Dict[str, Any]]:
    """Validate and deterministically merge duplicate Arena IDs.

    Duplicate IDs use the maximum observed quantity. Quantities above four are
    retained but reported because a few Arena records can be non-standard; they
    must not be silently discarded.
    """
    merged: Dict[int, int] = {}
    duplicate_ids: List[int] = []
    nonstandard_quantities: List[Dict[str, int]] = []
    for index, raw in enumerate(raw_cards):
        if index >= MAX_SNAPSHOT_CARDS:
            raise SnapshotValidationError(
                f"A collection snapshot may contain at most {MAX_SNAPSHOT_CARDS:,} card records."
            )
        arena_value = raw.get("arenaId", raw.get("arena_id", raw.get("grpId", raw.get("id"))))
        quantity_value = raw.get("quantity", raw.get("owned", raw.get("count", raw.get("qty"))))
        try:
            arena_id = int(arena_value)
            quantity = int(quantity_value)
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError(f"Invalid card record at index {index}: {raw}") from exc
        if arena_id <= 0:
            raise SnapshotValidationError(f"Arena ID must be positive: {arena_id}")
        if quantity <= 0 or quantity > 255:
            raise SnapshotValidationError(
                f"Quantity for Arena ID {arena_id} must be between 1 and 255: {quantity}"
            )
        if quantity > 4:
            nonstandard_quantities.append({"arenaId": arena_id, "quantity": quantity})
        if arena_id in merged:
            duplicate_ids.append(arena_id)
            merged[arena_id] = max(merged[arena_id], quantity)
        else:
            merged[arena_id] = quantity
    cards = [CollectionCard(arena_id=arena_id, quantity=quantity) for arena_id, quantity in sorted(merged.items())]
    return cards, {
        "duplicateArenaIds": sorted(set(duplicate_ids)),
        "nonstandardQuantities": nonstandard_quantities,
    }


def build_snapshot(
    *,
    source: str,
    cards: Iterable[Mapping[str, Any]],
    captured_at: Optional[str] = None,
    mtga_client_version: Optional[str] = None,
    diagnostics: Optional[Mapping[str, Any]] = None,
) -> CollectionSnapshot:
    if source not in AUTHORITATIVE_SOURCES:
        raise SnapshotValidationError(
            f"Ownership snapshot source must be one of {sorted(AUTHORITATIVE_SOURCES)}; got {source!r}."
        )
    normalized, validation = normalize_cards(cards)
    if not normalized:
        raise SnapshotValidationError("An empty collection snapshot cannot replace the saved collection.")
    combined = dict(diagnostics or {})
    combined["validation"] = validation
    return CollectionSnapshot(
        source=source,
        cards=normalized,
        captured_at=captured_at or utc_now(),
        mtga_client_version=mtga_client_version,
        diagnostics=combined,
    )
