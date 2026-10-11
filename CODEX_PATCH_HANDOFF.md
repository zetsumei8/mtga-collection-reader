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

- Uses only the reviewed `1.0.11.0` release of `frcaton/mtga-tracker-daemon`; it never queries the moving `latest` release.
- Downloads `mtga-tracker-daemon-Windows.zip` on first use and verifies its fixed size and SHA-256 digest.
- Extracts the release under the Reader Data folder and checks the complete file tree and executable against fixed SHA-256 values.
- Re-verifies cached helper files on each sync and immediately before launch; cached `current.json` metadata does not select the executable.
- Preserves a cache that fails verification in a uniquely named quarantine folder before replacing it.
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

- Adopt a newer helper only in a Reader release after compatibility and security review, then update the pinned archive, executable, and tree hashes together.
- Add a visible helper version/update control only if it retains the same explicit release review and integrity checks.
- Add a Windows integration test harness that substitutes a fixture daemon for deterministic UI testing.
