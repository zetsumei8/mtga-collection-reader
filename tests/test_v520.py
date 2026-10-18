from __future__ import annotations

import io
import sys
import json
import hashlib
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import daemon_provider
import manual_import
import provider_contract
from bridge_service import BridgeHandler, ensure_token
from card_database import load_local_database, prepare_database
from collection_store import CollectionStore, SuspiciousSnapshotError
from daemon_provider import DaemonProviderError, HelperInstall, MtgaDaemonProvider, _extract_archive_safely, ensure_helper, parse_all_cards_payload, parse_cards_payload
from manual_import import ManualImportError, load_manual_snapshot
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

    original_limit = provider_contract.MAX_SNAPSHOT_CARDS
    provider_contract.MAX_SNAPSHOT_CARDS = 2
    try:
        build_snapshot(
            source="manual-import",
            cards=[{"arenaId": 1101, "quantity": 1}, {"arenaId": 1102, "quantity": 1}, {"arenaId": 1103, "quantity": 1}],
        )
    except SnapshotValidationError:
        pass
    else:
        raise AssertionError("oversized snapshot was accepted")
    finally:
        provider_contract.MAX_SNAPSHOT_CARDS = original_limit


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

    too_large = root / "too-large.json"
    too_large.write_text("{}")
    original_limit = manual_import.MAX_MANUAL_IMPORT_BYTES
    manual_import.MAX_MANUAL_IMPORT_BYTES = 1
    try:
        load_manual_snapshot(too_large)
    except ManualImportError:
        pass
    else:
        raise AssertionError("oversized manual import was accepted")
    finally:
        manual_import.MAX_MANUAL_IMPORT_BYTES = original_limit


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
    # This fallback test must stay deterministic on machines with Arena installed.
    with mock.patch("card_database.discover_mtga_raw_paths", return_value=()):
        database, source = prepare_database(
            root / "missing-raw", root / "arena_cards.json", None, fallback_database=fallback
        )
    assert source == "Arena helper card database"
    assert database[3101]["name"] == "Fallback Card"
    assert (root / "arena_cards.json").is_file()


def test_local_database_localization_schemas(root: Path) -> None:
    for schema in ("arena-enus", "legacy"):
        raw = root / schema / "Raw"
        raw.mkdir(parents=True)
        database_path = raw / "Raw_CardDatabase_test.mtga"
        connection = sqlite3.connect(database_path)
        connection.execute(
            "CREATE TABLE Cards (GrpId INT, TitleId INT, ExpansionCode TEXT, CollectorNumber TEXT)"
        )
        connection.execute(
            "INSERT INTO Cards VALUES (4201, 5101, 'TST', '42')"
        )
        if schema == "arena-enus":
            connection.execute("CREATE TABLE Localizations (LocId INT)")
            connection.execute(
                "CREATE TABLE Localizations_enUS (LocId INT, Formatted INT, Loc TEXT)"
            )
            connection.executemany(
                "INSERT INTO Localizations_enUS VALUES (?, ?, ?)",
                [
                    (5101, 0, "Card rules text"),
                    (5101, 1, "Test Card"),
                ],
            )
        else:
            connection.execute(
                "CREATE TABLE Localizations (Id INT, Text TEXT, Format TEXT)"
            )
            connection.execute(
                "INSERT INTO Localizations VALUES (5101, 'Test Card', 'en-US')"
            )
        connection.execute("CREATE TABLE Padding (Value BLOB)")
        connection.execute("INSERT INTO Padding VALUES (?)", (b"x" * 600_000,))
        connection.commit()
        connection.close()

        database = load_local_database(raw, None)
        assert database[4201] == {
            "name": "Test Card",
            "set": "TST",
            "collector_number": "42",
        }


def test_helper_install(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    executable_bytes = b"fixture executable" * 40
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as zipped:
        zipped.writestr("bin/mtga-tracker-daemon.exe", executable_bytes)
        zipped.writestr("bin/helper.dll", b"fixture dependency")
    archive_bytes = archive_buffer.getvalue()
    expected_root = root / "expected"
    (expected_root / "bin").mkdir(parents=True)
    (expected_root / "bin" / "mtga-tracker-daemon.exe").write_bytes(executable_bytes)
    (expected_root / "bin" / "helper.dll").write_bytes(b"fixture dependency")
    archive_hash = hashlib.sha256(archive_bytes).hexdigest()
    tree_hash = daemon_provider._helper_tree_sha256(expected_root)
    executable_hash = hashlib.sha256(executable_bytes).hexdigest()

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
        assert url == daemon_provider.PINNED_DOWNLOAD_URL, "an unpinned helper URL was requested"
        return FakeResponse(body=archive_bytes)

    old_name = daemon_provider.os.name
    old_get = daemon_provider.requests.get
    daemon_provider.os.name = "nt"
    daemon_provider.requests.get = fake_get
    try:
        tools_root = root / "tools" / "mtga-tracker-daemon"
        tools_root.mkdir(parents=True)
        external_exe = root / "other-version" / "mtga-tracker-daemon.exe"
        external_exe.parent.mkdir()
        external_exe.write_bytes(b"must not be trusted")
        (tools_root / "current.json").write_text(
            json.dumps({"tag": "1.0.12.0", "executable": str(external_exe)}),
            encoding="utf-8",
        )

        with mock.patch.object(daemon_provider, "PINNED_ARCHIVE_SIZE", len(archive_bytes)), \
             mock.patch.object(daemon_provider, "PINNED_SHA256", archive_hash), \
             mock.patch.object(daemon_provider, "PINNED_TREE_SHA256", tree_hash), \
             mock.patch.object(daemon_provider, "PINNED_EXECUTABLE_SIZE", len(executable_bytes)), \
             mock.patch.object(daemon_provider, "PINNED_EXECUTABLE_SHA256", executable_hash):
            install = ensure_helper(root)
            assert install.tag == daemon_provider.PINNED_TAG == "1.0.11.0"
            assert install.download_url == daemon_provider.PINNED_DOWNLOAD_URL
            assert install.sha256 == archive_hash
            assert install.executable.is_file()
            assert calls == [daemon_provider.PINNED_DOWNLOAD_URL]

            again = ensure_helper(root)
            assert again.executable == install.executable
            assert calls == [daemon_provider.PINNED_DOWNLOAD_URL]

            install.executable.write_bytes(b"tampered executable")
            repaired = ensure_helper(root)
            assert repaired.executable == install.executable
            assert repaired.executable.read_bytes() == executable_bytes
            assert calls == [daemon_provider.PINNED_DOWNLOAD_URL] * 2

        quarantined = list(tools_root.glob("1.0.11.0.rejected-*"))
        assert len(quarantined) == 1
        assert (quarantined[0] / "bin" / "mtga-tracker-daemon.exe").read_bytes() == b"tampered executable"
        assert external_exe.read_bytes() == b"must not be trusted"
        assert json.loads((tools_root / "current.json").read_text(encoding="utf-8"))["tag"] == "1.0.11.0"
    finally:
        daemon_provider.os.name = old_name
        daemon_provider.requests.get = old_get


def test_launch_rehashes_helper_before_start(root: Path) -> None:
    data_dir = root / "data"
    install_root = daemon_provider._install_root(data_dir)
    executable = install_root / daemon_provider.PINNED_EXECUTABLE
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture executable")
    dependency = executable.parent / "helper.dll"
    dependency.write_bytes(b"fixture dependency")
    tree_hash = daemon_provider._helper_tree_sha256(install_root)
    executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
    dependency.write_bytes(b"tampered dependency")
    install = HelperInstall(
        daemon_provider.PINNED_TAG,
        executable,
        daemon_provider.PINNED_DOWNLOAD_URL,
        daemon_provider.PINNED_SHA256,
    )

    with mock.patch.object(daemon_provider, "PINNED_TREE_SHA256", tree_hash), \
         mock.patch.object(daemon_provider, "PINNED_EXECUTABLE_SIZE", len(b"fixture executable")), \
         mock.patch.object(daemon_provider, "PINNED_EXECUTABLE_SHA256", executable_hash), \
         mock.patch.object(daemon_provider.subprocess, "Popen") as popen:
        process = daemon_provider.DaemonProcess(install, data_dir)
        try:
            try:
                process.start()
            except DaemonProviderError as exc:
                assert "reviewed 1.0.11.0 release" in str(exc)
            else:
                raise AssertionError("a modified helper was started")
            popen.assert_not_called()
        finally:
            process.session.close()


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
    assert [row["arena_id"] for row in store.collection_rows(search="5005")] == [5005]

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
        shutdown = urllib.request.Request(base + "/v1/shutdown", method="POST")
        try:
            urllib.request.urlopen(shutdown, timeout=2)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("bridge accepted shutdown without a token")
        request = urllib.request.Request(
            base + "/v1/collection", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            assert response.headers["X-Content-Type-Options"] == "nosniff"
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
        test_local_database_localization_schemas(root / "local-database")
        test_helper_install(root / "helper")
        test_launch_rehashes_helper_before_start(root / "launch")
        test_helper_archive_safety(root / "archive")
        test_mocked_daemon_snapshot(root / "mock")
        test_snapshot_store(root / "store")
        test_bridge_token_and_http(root / "bridge")
    print("v5.2.5 validation: 11 test groups passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
