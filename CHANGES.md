# Changes

## 5.2.5 hardening update

- Pinned automatic sync to the reviewed `mtga-tracker-daemon` 1.0.11.0 release instead of selecting the moving GitHub `latest` release.
- Verify the fixed archive digest, complete installed helper file tree, and executable hash before use and immediately before launch.
- Ignore cached version metadata for executable selection and preserve any helper cache that fails verification for diagnosis.

## 5.2.0

- Replaced screenshot/anchor heuristic memory scanning with the maintained `mtga-tracker-daemon` collection endpoint.
- Reduced normal use to one button after Arena reaches the Home screen.
- Removed collection scrolling, screenshot capture, OCR, quantity-diamond analysis, Player.log collection parsing, and anchor-card entry from the normal workflow.
- Added automatic first-use download and extraction of the unmodified upstream Windows helper.
- Runs the helper on a random localhost port and stops it after each sync.
- Added tolerant parsing for camelCase and PascalCase `/cards` payloads.
- Preserved SQLite snapshots, suspicious-drop protection, unknown Arena IDs, rollback history, exports, manual JSON import, and the authenticated WB Fox bridge.
- Reduced installation dependencies to `requests` only.

## 5.1.0

- Previous experimental screenshot-assisted memory calibration release.
