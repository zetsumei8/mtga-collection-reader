# Third-Party Notices

## mtga-tracker-daemon

Repository: `frcaton/mtga-tracker-daemon`

License: GNU General Public License version 3.

The Reader downloads the upstream Windows release unmodified on first use and runs it as an independent local process. It is not bundled in this ZIP and is not linked into the Reader. The Reader communicates with it through its documented localhost HTTP API.

The upstream helper exposes `/status`, `/cards`, and other endpoints. The Reader uses only `/status`, `/cards`, and `/shutdown` for automatic collection sync.

Users can inspect the downloaded release metadata in:

```text
<runtime>\Data\tools\mtga-tracker-daemon\current.json
```
