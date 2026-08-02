# Patch Notes — 5.2.0

This patch changes the ownership provider, not the persistent collection format.

## Removed from normal sync

- Collection screen selection
- Card-grid scrolling
- Screenshot calibration
- OCR
- Quantity-diamond detection
- Manual anchor cards
- Player.log collection reconstruction
- Raw process-wide pair-pattern scanning

## Added

- First-use installation of the upstream Windows memory helper
- Random localhost helper port
- `/status` readiness check
- `/cards` full ownership retrieval
- Automatic helper shutdown after sync
- Small installer footprint

Existing `collection.db`, snapshots, settings, and exports are preserved.
