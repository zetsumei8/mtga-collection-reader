"""Local authenticated collection API service.

The service binds only to 127.0.0.1 and serves the latest accepted collection
snapshot. It does not expose Player.log, Arena credentials, or process memory.
"""
from __future__ import annotations

import argparse
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from collection_store import CollectionStore


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6842


def ensure_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    temp = path.with_suffix(".tmp")
    temp.write_text(token, encoding="utf-8")
    temp.replace(path)
    return token


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "MTGA Collection API/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = getattr(self.server, "bridge_token", "")
        header = self.headers.get("Authorization", "")
        return bool(expected) and secrets.compare_digest(header, f"Bearer {expected}")

    def do_GET(self) -> None:  # noqa: N802
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            self._json(403, {"error": "local requests only"})
            return
        if not self._authorized():
            self._json(401, {"error": "missing or invalid bridge token"})
            return
        store: CollectionStore = getattr(self.server, "collection_store")
        if self.path == "/v1/status":
            latest = store.latest_snapshot()
            self._json(
                200,
                {
                    "service": "wb-fox-bridge",
                    "schemaVersion": 1,
                    "hasCollection": latest is not None,
                    "latestSnapshotId": latest.get("snapshot_id") if latest else None,
                    "source": latest.get("source") if latest else None,
                    "capturedAt": latest.get("captured_at") if latest else None,
                },
            )
            return
        if self.path == "/v1/collection":
            latest = store.latest_snapshot()
            if latest is None:
                self._json(404, {"error": "no accepted collection snapshot"})
                return
            self._json(200, latest["payload"])
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/shutdown" and self._authorized():
            self._json(202, {"status": "shutdown accepted"})
            self.server.shutdown()
            return
        self._json(404, {"error": "not found"})


def run_server(data_dir: Path, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("The Collection API may bind only to localhost.")
    data_dir = Path(data_dir)
    store = CollectionStore(data_dir / "collection.db")
    token_path = data_dir / "bridge_token.txt"
    token = ensure_token(token_path)
    server = ThreadingHTTPServer((host, int(port)), BridgeHandler)
    setattr(server, "collection_store", store)
    setattr(server, "bridge_token", token)
    print(f"MTGA Collection API listening on http://{host}:{port}")
    print(f"Authentication token: {token_path}")
    print("Only the latest accepted collection snapshot is exposed.")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local MTGA Collection API service.")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    run_server(args.data_dir, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
