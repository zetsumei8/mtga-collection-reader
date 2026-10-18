# Third-Party Notices

## mtga-tracker-daemon

Repository: `frcaton/mtga-tracker-daemon`

License: GNU General Public License version 3.

The Reader downloads the upstream Windows release 1.0.11.0 unmodified on first use and runs it as an independent local process. It is not bundled in this ZIP and is not linked into the Reader. The Reader communicates with it through its documented localhost HTTP API. The release URL, archive size, and SHA-256 are fixed in this Reader version. Before use, the Reader verifies the complete installed file tree and the executable against the reviewed release hashes; it does not trust `current.json` to select a helper or executable.

The unmodified upstream helper may perform its own release check when it starts. The Reader's version pin controls which helper binary it installs; it does not change the helper's upstream behavior.

The upstream helper exposes `/status`, `/cards`, and other endpoints. The Reader uses only `/status`, `/cards`, and `/shutdown` for automatic collection sync.

Users can inspect the pinned helper metadata in:

```text
<runtime>\Data\tools\mtga-tracker-daemon\current.json
```
