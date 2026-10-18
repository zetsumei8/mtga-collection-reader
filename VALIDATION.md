# Validation record — 5.2.5

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
- Fixed helper URL, archive size, and archive SHA-256 verification
- Pinned helper file-tree and executable integrity checks
- Rejection and quarantine of a modified cached helper
- Cached metadata cannot redirect execution to another helper version or path
- Helper files are re-hashed immediately before process creation
- Mocked one-click helper provider snapshot
- Clean source compilation
- Clean ZIP extraction and compilation

## Windows checks still required

This build environment cannot run MTG Arena or a Windows executable. Before release designation, validate on Windows 11 with the current Arena client:

1. First-use download installs only pinned helper 1.0.11.0 after fixed archive and file-integrity checks pass.
2. A second sync reuses the same verified helper without querying GitHub's moving `latest` release.
3. If a cached helper file is modified, the Reader refuses to start it, preserves the cache, and installs a verified copy.
4. Windows Defender permits the known memory-reading helper or gives an actionable prompt.
5. The helper starts on a random localhost port without administrator rights.
6. `/status` identifies `MTGA.exe`.
7. `/cards` returns the complete owned collection.
8. The helper stops after the sync.
9. Repeated syncs are idempotent.
10. An Arena update failure preserves the last valid snapshot and recommends manual import.

Do not claim live Windows extraction is verified until these checks pass on the user's machine.
