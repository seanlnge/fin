from unittest.mock import Mock

import pytest

from fin import link_server
from fin.db import connect


def test_duplicate_is_rejected_before_exchange(tmp_path, monkeypatch):
    conn = connect(tmp_path / "fin.db")
    conn.execute("INSERT INTO items(item_id,institution,institution_id,access_token) VALUES('i','Bank','ins_1',X'00')")
    conn.commit()
    client = Mock()
    with pytest.raises(ValueError, match="already linked"):
        link_server.finish_link(conn, client, {"institution": {"institution_id": "ins_1"}, "public_token": "public-test"})
    client.item_public_token_exchange.assert_not_called()
    conn.close()


def test_exchange_encrypts_and_never_returns_token(tmp_path, monkeypatch):
    conn = connect(tmp_path / "fin.db")
    monkeypatch.setattr(link_server, "encrypt_token", lambda token: b"encrypted")
    client = Mock()
    client.item_public_token_exchange.return_value = {"item_id": "i", "access_token": "access-test"}
    result = link_server.finish_link(conn, client, {
        "institution": {"institution_id": "ins_1", "name": "Bank"}, "public_token": "public-test"})
    assert result == {"item_id": "i", "institution": "Bank", "status": "linked"}
    assert conn.execute("SELECT access_token FROM items").fetchone()[0] == b"encrypted"
    conn.close()


def test_hosted_link_requests_history_and_optional_liabilities(tmp_path, monkeypatch):
    conn = connect(tmp_path / "fin.db")
    monkeypatch.setattr(link_server, "encrypt_token", lambda token: b"encrypted")
    monkeypatch.setattr(link_server.webbrowser, "open", lambda url: True)
    client = Mock()
    client.link_token_create.return_value = {"hosted_link_url": "https://secure.plaid.com/test", "link_token": "link-test"}
    client.link_token_get.return_value = {"link_sessions": [{"finished_at": "now", "results": {
        "item_add_results": [{"institution": {"institution_id": "ins_1", "name": "Bank"}, "public_token": "public-test"}]}}]}
    client.item_public_token_exchange.return_value = {"item_id": "i", "access_token": "access-test"}
    assert link_server.link(conn, client)["status"] == "linked"
    request = client.link_token_create.call_args.args[0].to_dict()
    assert request["transactions"]["days_requested"] == 730
    assert [str(p) for p in request["products"]] == ["transactions"]
    assert [str(p) for p in request["optional_products"]] == ["liabilities"]
    conn.close()


def test_import_reuses_existing_item(tmp_path, monkeypatch):
    conn = connect(tmp_path / "fin.db")
    monkeypatch.setattr(link_server, "encrypt_token", lambda token: b"encrypted")
    client = Mock()
    client.item_get.return_value = {"item": {"item_id": "existing", "institution_id": "ins_1"}}
    client.institutions_get_by_id.return_value = {"institution": {"name": "Bank"}}
    result = link_server.import_item(conn, client, "access-sandbox-test")
    assert result["status"] == "imported"
    client.item_public_token_exchange.assert_not_called()
    assert conn.execute("SELECT access_token FROM items").fetchone()[0] == b"encrypted"
    conn.close()


def test_update_preserves_item_and_never_exchanges_token(tmp_path, monkeypatch):
    conn = connect(tmp_path / "fin.db")
    conn.execute("INSERT INTO items(item_id,institution,institution_id,access_token,status) VALUES('i','Bank','ins_1',X'00','login_required')")
    conn.commit()
    monkeypatch.setattr(link_server, "decrypt_token", lambda token: "access-sandbox-test")
    monkeypatch.setattr(link_server.webbrowser, "open", lambda url: True)
    client = Mock()
    client.link_token_create.return_value = {"hosted_link_url": "https://secure.plaid.com/test", "link_token": "link-test"}
    client.link_token_get.return_value = {"link_sessions": [{"finished_at": "now", "on_success": {"metadata": {}}}]}
    assert link_server.link(conn, client, update="i")["status"] == "reauthenticated"
    request = client.link_token_create.call_args.args[0].to_dict()
    assert "products" not in request
    assert request["access_token"] == "access-sandbox-test"
    assert request["additional_consented_products"] == ["liabilities"]
    client.item_public_token_exchange.assert_not_called()
    assert conn.execute("SELECT status FROM items").fetchone()[0] == "ok"
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 1
    conn.close()


@pytest.fixture
def update_flow(tmp_path, monkeypatch):
    conn = connect(tmp_path / "fin.db")
    conn.execute("""INSERT INTO items(item_id,institution,institution_id,access_token,status,last_synced_at,liabilities_synced_at)
                    VALUES('i','Bank','ins_1',X'00','login_required','2026-10-20','2026-10-20')""")
    conn.commit()
    monkeypatch.setattr(link_server, "decrypt_token", lambda token: "access-sandbox-test")
    monkeypatch.setattr(link_server.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(link_server.time, "sleep", lambda seconds: None)
    client = Mock()
    client.link_token_create.return_value = {"hosted_link_url": "https://secure.plaid.com/test", "link_token": "link-test"}
    yield conn, client
    conn.close()


def test_update_handoff_without_legacy_success(update_flow):
    conn, client = update_flow
    client.link_token_get.return_value = {"link_sessions": [{
        "finished_at": "now", "on_success": None, "events": [{"event_name": "HANDOFF"}],
        "results": {"item_add_results": []},
    }]}
    result = link_server.link(conn, client, update="i")
    assert result["confirmation"] == "HANDOFF"
    assert result["status"] == "reauthenticated"
    row = conn.execute("SELECT status,last_synced_at,liabilities_synced_at,access_token FROM items").fetchone()
    assert tuple(row) == ("ok", None, None, b"\x00")
    client.item_public_token_exchange.assert_not_called()


def test_update_waits_for_delayed_success_metadata(update_flow):
    conn, client = update_flow
    client.link_token_get.side_effect = [
        {"link_sessions": [{"finished_at": "now", "results": {}, "events": []}]},
        {"link_sessions": [{"finished_at": "now", "events": [{"event_name": "HANDOFF"}]}]},
    ]
    assert link_server.link(conn, client, update="i")["status"] == "reauthenticated"
    assert client.link_token_get.call_count == 2


@pytest.mark.parametrize("exit_metadata", [{"exit": {}}, {"on_exit": {}}, {"events": [{"event_name": "EXIT"}]}])
def test_explicit_update_exit_does_not_mark_success(update_flow, exit_metadata):
    conn, client = update_flow
    client.link_token_get.return_value = {"link_sessions": [{"finished_at": "now", **exit_metadata}]}
    with pytest.raises(ValueError, match="without confirmed completion"):
        link_server.link(conn, client, update="i")
    assert conn.execute("SELECT status FROM items").fetchone()[0] == "login_required"
    client.item_public_token_exchange.assert_not_called()


def test_earlier_exit_does_not_hide_resumed_success(update_flow):
    conn, client = update_flow
    client.link_token_get.side_effect = [
        {"link_sessions": [{"finished_at": "earlier", "exit": {}}, {"finished_at": None}]},
        {"link_sessions": [{"finished_at": "earlier", "exit": {}}, {"finished_at": "now", "events": []}]},
        {"link_sessions": [{"finished_at": "earlier", "exit": {}},
                           {"finished_at": "now", "events": [{"event_name": "HANDOFF"}]}]},
    ]
    assert link_server.link(conn, client, update="i")["status"] == "reauthenticated"


def test_finished_timestamp_alone_is_not_failure_or_success(update_flow, monkeypatch):
    conn, client = update_flow
    clock = iter([0, 0, 10])
    monkeypatch.setattr(link_server.time, "monotonic", lambda: next(clock))
    client.link_token_get.return_value = {"link_sessions": [{"finished_at": "now", "events": []}]}
    with pytest.raises(TimeoutError, match="completion metadata"):
        link_server.link(conn, client, update="i", timeout=5)
    assert conn.execute("SELECT status FROM items").fetchone()[0] == "login_required"


def test_failed_event_before_handoff_is_recoverable(update_flow):
    conn, client = update_flow
    client.link_token_get.return_value = {"link_sessions": [{"finished_at": "now", "events": [
        {"event_name": "ERROR", "event_metadata": {"error_message": "private details"}},
        {"event_name": "HANDOFF"},
    ]}]}
    result = link_server.link(conn, client, update="i")
    assert result["status"] == "reauthenticated"
    assert "private" not in str(result)
