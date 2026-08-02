# MTGA Collection Reader

A standalone Windows archive and export tool for **your MTG Arena card collection**.

> This is not a deck builder. It imports, preserves, and exports collection snapshots; it does not create, edit, evaluate, or publish decks.

## What it does

- One-click read-only collection sync when MTG Arena is open and at the Home screen.
- Stores every accepted snapshot locally in SQLite, so a working collection history is never silently replaced.
- Exports JSON, CSV, and text collection files.
- Accepts manual JSON snapshots when an Arena update temporarily affects automatic sync.
- Protects against empty imports and suspicious large collection drops.
- Optionally exposes the latest snapshot through a token-protected, localhost-only JSON API for tools you choose to connect.

The reader never asks for a Wizards password, changes Arena files, writes to Arena memory, or captures screenshots. It uses the unmodified upstream `frcaton/mtga-tracker-daemon` helper for read-only process access; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Install and use

1. Download a release and extract it to a normal folder.
2. Run `install.bat` and choose a runtime location.
3. Open MTG Arena, sign in, and wait for the Home screen.
4. Run `run_reader.bat`, then click **Get My MTGA Collection**.

Your private data is kept separately from the application files:

- Database: `<runtime>\Data\collection.db`
- Exports: `<runtime>\Exports`
- Helper diagnostics: `<runtime>\Data\logs\mtga-memory-helper.log`

The first automatic sync needs internet access to download the helper; later syncs reuse the local copy. Manual import remains available at all times.

## Manual snapshot format

```json
{
  "schemaVersion": 1,
  "source": "manual-import",
  "cards": [{"arenaId": 102646, "quantity": 3}]
}
```

The importer also accepts `[ {"grpId": 102646, "owned": 3} ]` and `{ "102646": 3 }`.

## Local collection API (optional)

Use **Start local Collection API** only if another local tool needs your latest snapshot. It listens on `127.0.0.1:6842`, requires the token in `Data\bridge_token.txt`, and provides:

- `GET /v1/status`
- `GET /v1/collection`

It is disabled until you start it and exposes no Arena credentials or process data.

## Development

Requires 64-bit CPython 3.10+.

```powershell
python -m pip install -r requirements.txt
python tests/test_v520.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and [CHANGES.md](CHANGES.md). MTG Arena and Magic: The Gathering are trademarks of Wizards of the Coast; this project is unofficial and unaffiliated.
