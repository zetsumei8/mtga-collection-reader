# Validation record — 5.2.0

## Automated checks

- Canonical snapshot validation
- Manual JSON formats
- SQLite idempotent merge and suspicious-drop protection
- Unknown Arena ID retention
- Authenticated WB Fox bridge
- CamelCase helper card payload parsing
- PascalCase helper card payload parsing
- Duplicate Arena ID merge
- Empty helper collection rejection
- Mocked one-click helper provider snapshot
- Clean source compilation
- Clean ZIP extraction and compilation

## Windows checks still required

This build environment cannot run MTG Arena or a Windows executable. Before release designation, validate on Windows 11 with the current Arena client:

1. First-use helper download succeeds.
2. Windows Defender permits the known memory-reading helper or gives an actionable prompt.
3. The helper starts on a random localhost port without administrator rights.
4. `/status` identifies `MTGA.exe`.
5. `/cards` returns the complete owned collection.
6. The helper stops after the sync.
7. Repeated syncs are idempotent.
8. An Arena update failure preserves the last valid snapshot and recommends manual import.

Do not claim live Windows extraction is verified until these checks pass on the user's machine.
