"""Manual JSON collection snapshot import."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from provider_contract import CollectionSnapshot, SnapshotValidationError, build_snapshot


class ManualImportError(RuntimeError):
    pass


def _records_from_payload(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, dict):
        cards = payload.get("cards")
        if isinstance(cards, list):
            return cards
        # Legacy Arena ID -> quantity map.
        numeric_records = []
        for key, value in payload.items():
            try:
                arena_id = int(key)
                quantity = int(value)
            except (TypeError, ValueError):
                continue
            numeric_records.append({"arenaId": arena_id, "quantity": quantity})
        if numeric_records:
            return numeric_records
    if isinstance(payload, list):
        return payload
    raise ManualImportError(
        "The JSON must contain a cards array, a list of card records, or an Arena-ID/quantity map."
    )


def load_manual_snapshot(path: Path) -> CollectionSnapshot:
    source_path = Path(path).expanduser()
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ManualImportError(f"Could not read {source_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManualImportError(f"Invalid JSON in {source_path}: {exc}") from exc

    captured_at = payload.get("capturedAt") if isinstance(payload, dict) else None
    mtga_version = payload.get("mtgaClientVersion") if isinstance(payload, dict) else None
    try:
        return build_snapshot(
            source="manual-import",
            cards=_records_from_payload(payload),
            captured_at=str(captured_at) if captured_at else None,
            mtga_client_version=str(mtga_version) if mtga_version else None,
            diagnostics={"sourceFile": str(source_path)},
        )
    except SnapshotValidationError as exc:
        raise ManualImportError(str(exc)) from exc
