# Codex Patch Handoff — MTGA Collection Reader / WB Fox Bridge 5.2.0

## Product outcome

The user must be able to obtain their complete MTG Arena collection with the least practical friction:

```text
Open Arena → wait for Home → click Get My MTGA Collection
```

No Collection scrolling, screenshots, OCR, anchor cards, or Player.log collection reconstruction may be required in the normal path.

## Canonical provider order

1. `MtgaDaemonProvider` — primary automatic provider
2. `ManualJsonImportProvider` — permanent fallback
3. Player.log — supplemental activity only; it must never replace total ownership

## Current implementation

`daemon_provider.py`:

- Queries the latest GitHub release for `frcaton/mtga-tracker-daemon`.
- Falls back to pinned release `1.0.11.0`.
- Downloads `mtga-tracker-daemon-Windows.zip` on first use.
- Extracts it under the Reader Data folder.
- Stores source URL and SHA-256 in `current.json`.
- Starts the helper on a random localhost port.
- Polls `/status`.
- Reads `/cards`.
- Accepts both camelCase and PascalCase JSON names.
- Rejects empty results.
- Shuts down the helper after every attempt.

`mtg.py`:

- Presents one primary **Get My MTGA Collection** button.
- Loads identities only after the ownership snapshot is obtained.
- Applies the existing authoritative snapshot protections and exports.

## Non-negotiable safety behavior

- Never write to MTGA memory.
- Never intercept or alter game traffic.
- Never collect Wizards credentials or authentication tokens.
- Helper traffic must remain on localhost.
- Stop the helper after sync.
- Never overwrite the last valid collection with an empty result.
- Require confirmation for suspiciously large drops.
- Retain unknown Arena IDs and show mapping failures.
- Preserve every distinct accepted snapshot.
- Keep manual JSON import permanently.

## Licensing boundary

`mtga-tracker-daemon` is GPLv3. Keep it an independent, unmodified program downloaded from upstream. Do not copy its source into the Reader or link its assemblies into the Reader unless the resulting licensing obligations are intentionally accepted. Retain `THIRD_PARTY_NOTICES.md`.

## Immediate Windows validation

Run the exact checks in `VALIDATION.md`. The highest-priority unknown is whether upstream release 1.0.11.0 reads the current July 2026 MTGA build on this user's Windows 11 installation.

If `/cards` fails after an Arena update:

- Preserve the previous snapshot.
- Show: `Automatic collection sync is temporarily incompatible with this MTGA update.`
- Record helper logs and release metadata.
- Offer manual JSON import.
- Investigate the upstream helper before reintroducing screenshots or anchors.

## Future hardening

- Cache latest-release checks for 24 hours.
- Verify an upstream-published digest/signature when available.
- Add a visible helper version/update control.
- Add token-authenticated proxying only if the helper must remain running; current sync should remain short-lived.
- Add a Windows integration test harness that substitutes a fixture daemon for deterministic UI testing.
