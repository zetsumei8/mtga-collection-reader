# MTGA Collection Reader 5.2.5

## Your Arena collection, without a subscription

MTGA Collection Reader is a free, standalone way to export the Arena cards you own for any theorycrafting workflow. It does not build, recommend, or publish decks—and it does not lock your collection behind a subscription.

## Reliability and security

- Fixed SQLite connection cleanup that could lock the database on Windows.
- Added safe helper archive extraction and archive-size limits.
- Verifies helper downloads against GitHub-published SHA-256 digests before installation.
- Rejects oversized manual imports and implausibly large snapshots.
- Tightened the optional localhost Collection API: authenticated shutdown, no-cache and browser-hardening headers, and private token-file permissions where supported.
- Added offline regression coverage and GitHub Actions validation.

## Upgrade

Extract the release and run `install.bat`. Existing runtime data, snapshots, exports, and helper cache remain separate from application files.

## Release verification

Run:

```powershell
Get-FileHash .\MTGA_Collection_Reader_v5.2.5_ReleaseReady.zip -Algorithm SHA256
```

Compare it with the SHA-256 published alongside the release.
