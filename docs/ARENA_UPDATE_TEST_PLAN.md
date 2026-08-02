# Arena update compatibility test plan

Run this after each MTG Arena client update and before promoting a Reader release.

1. Record the Reader version, Arena version, Windows version, and test time.
2. Start Arena, sign in to a non-production test account when available, and wait at the Home screen.
3. In the separate MTGA Collection Reader app, run **Get My MTGA Collection**.
4. Confirm a successful snapshot, sensible unique-card/copy totals, and generated JSON/CSV/text exports.
5. Run the sync again; confirm the snapshot is unchanged and no duplicate history or database lock appears.
6. Stop Arena and retry. Confirm the Reader fails safely, retains the last accepted snapshot, and suggests manual JSON import.
7. Test a deliberately small manual snapshot. Confirm suspicious-drop confirmation is required and canceling preserves the accepted collection.
8. Start the optional Collection API; verify `/v1/collection` returns 401 without its bearer token and returns the latest snapshot with it.
9. Record only sanitized results. Never commit exports, `collection.db`, `bridge_token.txt`, helper logs, or account details.

Promotion gate: all steps pass, no collection is silently replaced, and the helper’s downloaded SHA-256 matches GitHub release metadata.
