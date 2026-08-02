from __future__ import annotations

import io
import sys
import json
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import daemon_provider
from bridge_service import BridgeHandler, ensure_token
from card_database import prepare_database
from collection_store import CollectionStore, SuspiciousSnapshotError
from daemon_provider import DaemonProviderError, HelperInstall, MtgaDaemonProvider, _extract_archive_safely, ensure_helper, parse_all_cards_payload, parse_cards_payload
from manual_import import load_manual_snapshot
from provider_contract import SnapshotValidationError, build_snapshot


def observations(snapshot, known=True):
    return [
        {
            "arena_id": card.arena_id,
            "name": f"Card {card.arena_id}" if known else f"Unknown Arena card {card.arena_id}",
            "set_code": "TST" if known else "",
            "collector_number": str(card.arena_id),
            "quantity": card.quantity,
            "confidence": 1.0 if known else 0.0,
            "needs_review": not known,
            "review_reason": "Unknown ID" if not known else "",
        }
        for card in snapshot.cards
    ]


def test_provider_validation() -> None:
    snapshot = build_snapshot(
        source="manual-import",
        cards=[
            {"arenaId": 1001, "quantity": 1},
            {"arenaId": 1001, "quantity": 3},
            {"arenaId": 1002, "quantity": 5},
        ],
    )
    assert snapshot.unique_cards == 2
    assert snapshot.total_copies == 8
    assert snapshot.cards[0].quantity == 3
    try:
        build_snapshot(source="manual-import", cards=[])
    except SnapshotValidationError:
        pass
    else:
        raise AssertionError("empty snapshot was accepted")


def test_manual_formats(root: Path) -> None:
    canonical = root / "canonical.json"
    canonical.write_text(json.dumps({"schemaVersion": 1, "cards": [{"arenaId": 2001, "quantity": 2}]}))
    assert load_manual_snapshot(canonical).cards[0].arena_id == 2001

    record_list = root / "list.json"
    record_list.write_text(json.dumps([{"grpId": 2002, "owned": 4}]))
    assert load_manual_snapshot(record_list).cards[0].quantity == 4

    numeric = root / "map.json"
    numeric.write_text(json.dumps({"2003": 1, "2004": 2}))
    assert load_manual_snapshot(numeric).unique_cards == 2


def test_helper_payloads() -> None:
    camel = parse_cards_payload(
        {"cards": [{"grpId": 3001, "owned": 1}, {"grpId": 3002, "owned": 4}]}
    )
    assert camel == [{"arenaId": 3001, "quantity": 1}, {"arenaId": 3002, "quantity": 4}]

    pascal = parse_cards_payload(
        {"Cards": [{"GrpId": 3001, "Owned": 2}, {"GrpId": 3001, "Owned": 3}]}
    )
    assert pascal == [{"arenaId": 3001, "quantity": 3}]

    identities = parse_all_cards_payload(
        {"Cards": [{"GrpId": 3001, "Title": "Test Card", "ExpansionCode": "tst"}]}
    )
    assert identities[3001]["name"] == "Test Card"
    assert identities[3001]["set"] == "TST"

    try:
        parse_cards_payload({"Cards": []})
    except DaemonProviderError:
        pass
    else:
        raise AssertionError("empty helper collection was accepted")


def test_helper_identity_fallback(root: Path) -> None:
    fallback = {
        3101: {"name": "Fallback Card", "set": "TST", "collector_number": "1"}
    }
    database, source = prepare_database(
        root / "missing-raw", root / "arena_cards.json", None, fallback_database=fallback
    )
    assert source == "Arena helper card database"
    assert database[3101]["name"] == "Fallback Card"
    assert (root / "arena_cards.json").is_file()


def test_helper_install(root: Path) -> None:
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as zipped:
        zipped.writestr("bin/mtga-tracker-daemon.exe", b"fixture executable")
    archive_bytes = archive_buffer.getvalue()

    class FakeResponse:
        def __init__(self, payload=None, body=b""):
            self._payload = payload
            self._body = body
            self.headers = {"Content-Length": str(len(body))}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

        def iter_content(self, chunk_size=1024):
            for index in range(0, len(self._body), chunk_size):
                yield self._body[index:index + chunk_size]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        if url == daemon_provider.LATEST_RELEASE_API:
            return FakeResponse(
                {
                    "tag_name": "1.0.test",
                    "assets": [
                        {
                            "name": daemon_provider.WINDOWS_ASSET_NAME,
                            "browser_download_url": "https://example.invalid/helper.zip",
                        }
                    ],
                }
            )
        return FakeResponse(body=archive_bytes)

    old_name = daemon_provider.os.name
    old_get = daemon_provider.requests.get
    daemon_provider.os.name = "nt"
    daemon_provider.requests.get = fake_get
    try:
        install = ensure_helper(root)
        assert install.tag == "1.0.test"
        assert install.executable.is_file()
        assert install.sha256
        again = ensure_helper(root)
        assert again.executable == install.executable
        assert len(calls) >= 2  # release metadata + archive; marker reuse may still check metadata
    finally:
        daemon_provider.os.name = old_name
        daemon_provider.requests.get = old_get


def test_helper_archive_safety(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    archive = root / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../outside.txt", b"not allowed")
    try:
        _extract_archive_safely(archive, root / "staging")
    except DaemonProviderError:
        pass
    else:
        raise AssertionError("unsafe helper archive path was accepted")
    assert not (root / "outside.txt").exists()


def test_mocked_daemon_snapshot(root: Path) -> None:
    install = HelperInstall("fixture", root / "fake.exe", "fixture", "abc")

    class FakeProcess:
        def __init__(self, _install, _data_dir, _reporter):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def start(self):
            return {"IsRunning": True, "DaemonVersion": "fixture", "ProcessId": 4242}

        def get_json(self, endpoint, timeout=0):
            if endpoint == "/cards":
                return {
                    "Cards": [
                        {"GrpId": 4001, "Owned": 4},
                        {"GrpId": 4002, "Owned": 2},
                    ],
                    "ElapsedTime": 125,
                }
            assert endpoint == "/allCards"
            return {
                "Cards": [
                    {"GrpId": 4001, "Title": "First Card", "ExpansionCode": "AAA"},
                    {"GrpId": 4002, "Title": "Second Card", "ExpansionCode": "BBB"},
                ]
            }

    old_process = daemon_provider.DaemonProcess
    daemon_provider.DaemonProcess = FakeProcess
    try:
        provider = MtgaDaemonProvider(root)
        provider.install = install
        snapshot = provider.read_collection()
        assert snapshot.source == "mtga-memory"
        assert snapshot.unique_cards == 2
        assert snapshot.total_copies == 6
        assert snapshot.diagnostics["processId"] == 4242
        assert provider.identity_database[4001]["name"] == "First Card"
    finally:
        daemon_provider.DaemonProcess = old_process


def test_snapshot_store(root: Path) -> None:
    store = CollectionStore(root / "collection.db")
    first = build_snapshot(
        source="mtga-memory",
        cards=[{"arenaId": 5000 + index, "quantity": 4} for index in range(100)],
    )
    scan1 = store.begin_session("mtga-memory", "first")
    summary1, _ = store.apply_authoritative_snapshot(scan1, first, observations(first))
    store.finish_session(summary1)
    assert store.metrics()["unique_entries"] == 100

    scan2 = store.begin_session("mtga-memory", "same")
    summary2, _ = store.apply_authoritative_snapshot(scan2, first, observations(first))
    store.finish_session(summary2)
    assert summary2.unchanged_entries == 100
    assert len(store.snapshot_rows()) == 1

    small = build_snapshot(
        source="mtga-memory",
        cards=[{"arenaId": 5000 + index, "quantity": 1} for index in range(10)],
    )
    scan3 = store.begin_session("mtga-memory", "small")
    try:
        store.apply_authoritative_snapshot(scan3, small, observations(small))
    except SuspiciousSnapshotError:
        pass
    else:
        raise AssertionError("suspicious drop was applied without confirmation")

    unknown = build_snapshot(source="mtga-memory", cards=[{"arenaId": 999999, "quantity": 2}])
    scan4 = store.begin_session("mtga-memory", "unknown")
    summary4, _ = store.apply_authoritative_snapshot(
        scan4, unknown, observations(unknown, known=False), allow_suspicious_drop=True
    )
    store.finish_session(summary4)
    row = store.collection_rows()[0]
    assert row["arena_id"] == 999999
    assert row["needs_review"] == 1


def test_bridge_token_and_http(root: Path) -> None:
    token_path = root / "bridge_token.txt"
    token = ensure_token(token_path)
    assert token == ensure_token(token_path)

    store = CollectionStore(root / "bridge.db")
    snapshot = build_snapshot(source="manual-import", cards=[{"arenaId": 7777, "quantity": 2}])
    scan_id = store.begin_session("manual-import", "bridge")
    summary, _ = store.apply_authoritative_snapshot(scan_id, snapshot, observations(snapshot))
    store.finish_session(summary)

    server = ThreadingHTTPServer(("127.0.0.1", 0), BridgeHandler)
    setattr(server, "collection_store", store)
    setattr(server, "bridge_token", token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        try:
            urllib.request.urlopen(base + "/v1/collection", timeout=2)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("bridge accepted a request without a token")
        request = urllib.request.Request(
            base + "/v1/collection", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["cards"] == [{"arenaId": 7777, "quantity": 2}]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        test_provider_validation()
        test_manual_formats(root)
        test_helper_payloads()
        test_helper_identity_fallback(root / "identity")
        test_helper_install(root / "helper")
        test_helper_archive_safety(root / "archive")
        test_mocked_daemon_snapshot(root / "mock")
        test_snapshot_store(root / "store")
        test_bridge_token_and_http(root / "bridge")
    print("v5.2.1 validation: 9 test groups passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
