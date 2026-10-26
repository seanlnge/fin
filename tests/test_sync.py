"""Deterministic ingestion tests: synthetic tokens, in-memory SQLite, no network."""
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace

from plaid.api.plaid_api import PlaidApi
from plaid.exceptions import ApiException
from plaid.model.account_balance import AccountBalance
from plaid.model.account_base import AccountBase
from plaid.model.account_type import AccountType
from plaid.model.accounts_get_response import AccountsGetResponse
from plaid.model.transactions_sync_response import TransactionsSyncResponse
from plaid.model.transactions_update_status import TransactionsUpdateStatus
import pytest

from fin import crypto, sync
from fin.db import connect
from fin.plaid_client import build_client


class FakeClient:
    def __init__(self, **responses):
        self.responses = {name: deque(values) for name, values in responses.items()}
        self.calls = defaultdict(list)

    def __getattr__(self, name):
        def call(request):
            self.calls[name].append(request.to_dict())
            if name not in self.responses or not self.responses[name]:
                raise AssertionError(f"Unscripted fake endpoint: {name}")
            value = self.responses[name].popleft()
            if isinstance(value, Exception):
                raise value
            if callable(value):
                return value(request)
            return value
        return call


def error(code=None, *, status=400, error_type="ITEM_ERROR"):
    exc = ApiException(status=status, reason="secret must never appear")
    exc.body = json.dumps({"error_code": code, "error_type": error_type,
                           "error_message": "access-fake-secret", "access_token": "access-fake-secret"})
    return exc


def page(*, added=(), modified=(), removed=(), cursor="finished", more=False,
         history="HISTORICAL_UPDATE_COMPLETE"):
    return {"added": list(added), "modified": list(modified), "removed": list(removed),
            "next_cursor": cursor, "has_more": more, "transactions_update_status": history}


def account(account_id="a", *, currency="USD"):
    return {"account_id": account_id, "name": "Checking", "type": "depository",
            "subtype": "checking", "mask": "1234", "balances": {
                "current": 123.45, "available": None, "limit": None,
                "iso_currency_code": currency, "unofficial_currency_code": "BTC" if currency is None else None}}


def tx(transaction_id="t", *, account_id="a", amount=12.34, pending=False, pending_id=None):
    return {"transaction_id": transaction_id, "account_id": account_id,
            "date": date(2026, 10, 20), "amount": amount, "name": "Test purchase",
            "pending": pending, "pending_transaction_id": pending_id, "iso_currency_code": "USD",
            "personal_finance_category": {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_GROCERIES"}}


def stream(stream_id="s", account_id="a"):
    return {"stream_id": stream_id, "account_id": account_id, "merchant_name": "Subscription",
            "frequency": "MONTHLY", "average_amount": {"amount": 10.0, "iso_currency_code": "USD"},
            "last_amount": {"amount": 11.0}, "last_date": date(2026, 10, 1), "is_active": True}


def liability(account_id="a"):
    return {"account_id": account_id, "aprs": [{"apr_type": "purchase_apr", "apr_percentage": 19.9}],
            "minimum_payment_amount": 20.0, "next_payment_due_date": date(2026, 11, 1),
            "last_statement_balance": 100.0}


@pytest.fixture(autouse=True)
def isolated_keyring(monkeypatch):
    values = {}
    monkeypatch.setattr(crypto.keyring, "get_keyring", lambda: SimpleNamespace(priority=1))
    monkeypatch.setattr(crypto.keyring, "get_password", lambda service, key: values.get((service, key)))
    monkeypatch.setattr(crypto.keyring, "set_password", lambda service, key, value: values.__setitem__((service, key), value))
    return values


@pytest.fixture
def conn(tmp_path):
    db = connect(tmp_path / "fixture.db")
    db.execute("INSERT INTO items(item_id,institution,access_token,cursor) VALUES(?,?,?,?)",
               ("i", "Test Bank", crypto.encrypt_token("access-fake-secret"), "original"))
    db.commit()
    yield db
    db.close()


@pytest.fixture
def sleeps(monkeypatch):
    values = []
    monkeypatch.setattr(sync.time, "sleep", values.append)
    return values


def client_for(*pages, accounts=None, recurring=None, liabilities=None):
    return FakeClient(
        transactions_sync=list(pages or [page()]),
        accounts_get=[{"accounts": accounts if accounts is not None else [account()]}],
        transactions_recurring_get=[recurring if recurring is not None else {"inflow_streams": [], "outflow_streams": []}],
        liabilities_get=[liabilities if liabilities is not None else {"liabilities": {"credit": [], "student": [], "mortgage": []}}],
    )


def seed_optional(conn, item_id="i", account_id="a"):
    conn.execute("INSERT INTO accounts(account_id,item_id) VALUES(?,?)", (account_id, item_id))
    conn.execute("INSERT INTO recurring_streams(stream_id,account_id) VALUES(?,?)", ("old-" + account_id, account_id))
    conn.execute("INSERT INTO liabilities(account_id,min_payment) VALUES(?,1)", (account_id,))
    conn.commit()


def test_paginated_sync_and_replay(conn):
    client = client_for(page(added=[tx()], cursor="p1", more=True),
                        page(modified=[tx(amount=15)], added=[tx("gone")], removed=[{"transaction_id": "gone"}]))
    result = sync.sync_items(conn, client)["items"][0]
    assert result == {"item_id": "i", "status": "ok", "added": 2, "modified": 1, "removed": 1,
                      "balances": 1, "history_status": "HISTORICAL_UPDATE_COMPLETE", "warnings": []}
    assert [request["cursor"] for request in client.calls["transactions_sync"]] == ["original", "p1"]
    assert all(request["count"] == 500 for request in client.calls["transactions_sync"])
    row = conn.execute("SELECT * FROM transactions").fetchone()
    assert (row["transaction_id"], row["amount"], row["currency"], row["date"]) == ("t", 15, "USD", "2026-10-20")
    assert row["category"] == "FOOD_AND_DRINK"
    assert json.loads(row["raw_json"])["date"] == "2026-10-20"
    assert conn.execute("SELECT cursor FROM items").fetchone()[0] == "finished"
    assert conn.execute("SELECT currency FROM balance_snapshots").fetchone()[0] == "USD"
    # Upsert replay is idempotent; daily optional endpoints are not called again.
    replay = client_for(page(added=[tx(amount=15)]))
    sync.sync_items(conn, replay)
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert not replay.calls["transactions_recurring_get"]
    assert not replay.calls["liabilities_get"]


def test_no_liability_accounts_is_specific_and_does_not_undo_transactions(conn):
    seed_optional(conn)
    result = sync.sync_items(conn, client_for(page(added=[tx()]),
                            liabilities=error("NO_LIABILITY_ACCOUNTS")))["items"][0]
    assert result["status"] == "ok"
    assert result["warnings"] == [{"product": "liabilities", "code": "NO_LIABILITY_ACCOUNTS"}]
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM liabilities").fetchone()[0] == 1
    assert conn.execute("SELECT liabilities_synced_at FROM items").fetchone()[0] is None
    assert "access-fake-secret" not in json.dumps(result)


def test_mutation_discards_all_buffered_pages(conn, sleeps):
    client = client_for(page(added=[tx("discard")], cursor="p1", more=True), error(sync._MUTATION),
                        page(added=[tx("keep")], cursor="p2", more=True), page())
    assert sync.sync_items(conn, client)["items"][0]["added"] == 1
    assert [r["cursor"] for r in client.calls["transactions_sync"]] == ["original", "p1", "original", "p2"]
    assert [r[0] for r in conn.execute("SELECT transaction_id FROM transactions")] == ["keep"]
    assert sleeps == [1]


def test_mutation_restart_is_bounded(conn, sleeps):
    client = client_for(error(sync._MUTATION), error(sync._MUTATION), error(sync._MUTATION))
    result = sync.sync_items(conn, client)["items"][0]
    assert result["error"] == sync._MUTATION
    assert sleeps == [1, 2]
    assert len(client.calls["transactions_sync"]) == 3
    assert conn.execute("SELECT cursor FROM items").fetchone()[0] == "original"


@pytest.mark.parametrize("failure", ["unknown_account", "db_trigger", "accounts_error", "second_page"])
def test_core_failure_rolls_back_all_data_and_cursor(conn, failure):
    bad = tx("bad", account_id="missing") if failure == "unknown_account" else tx("bad")
    client = client_for(page(added=[tx("good"), bad]))
    if failure == "db_trigger":
        conn.execute("CREATE TRIGGER reject_bad BEFORE INSERT ON transactions WHEN NEW.transaction_id='bad' BEGIN SELECT RAISE(ABORT,'access-fake-secret'); END")
    elif failure == "accounts_error":
        client.responses["accounts_get"] = deque([error("INSTITUTION_DOWN")])
    elif failure == "second_page":
        client.responses["transactions_sync"] = deque([page(added=[tx("good")], cursor="p1", more=True), error("INSTITUTION_DOWN")])
    result = sync.sync_items(conn, client)
    assert result["items"][0]["status"] == "error"
    assert "access-fake-secret" not in json.dumps(result)
    assert conn.execute("SELECT cursor,last_synced_at FROM items").fetchone()[:] == ("original", None)
    for table in ("accounts", "transactions", "balance_snapshots"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    log = conn.execute("SELECT * FROM sync_log").fetchone()
    assert log["added"] == 0 and log["error"]
    assert "secret" not in log["error"]


def test_pending_replaced_even_when_pending_arrives_on_later_page(conn):
    client = client_for(page(added=[tx("posted", pending_id="pending")], cursor="p1", more=True),
                        page(added=[tx("pending", pending=True)]))
    sync.sync_items(conn, client)
    assert [r[0] for r in conn.execute("SELECT transaction_id FROM transactions")] == ["posted"]
    assert conn.execute("SELECT pending_transaction_id FROM transactions").fetchone()[0] == "pending"


def test_pending_replacement_from_previous_sync(conn):
    sync.sync_items(conn, client_for(page(added=[tx("pending", pending=True)])))
    sync.sync_items(conn, client_for(page(added=[tx("posted", pending_id="pending")], removed=[{"transaction_id": "pending"}])))
    assert [r[0] for r in conn.execute("SELECT transaction_id FROM transactions")] == ["posted"]


@pytest.mark.parametrize("product", ["recurring", "liabilities"])
@pytest.mark.parametrize("code", ["PRODUCT_NOT_READY", "PRODUCT_NOT_SUPPORTED", "ITEM_LOGIN_REQUIRED"])
def test_optional_failure_preserves_old_rows_and_transactions(conn, product, code):
    seed_optional(conn)
    client = client_for(page(added=[tx()]), **{product: error(code)})
    result = sync.sync_items(conn, client)["items"][0]
    assert result["status"] == "ok"
    assert result["warnings"] == [{"product": product, "code": code}]
    table = "recurring_streams" if product == "recurring" else "liabilities"
    assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    assert conn.execute(f"SELECT {product}_synced_at FROM items").fetchone()[0] is None
    other = "liabilities" if product == "recurring" else "recurring"
    assert conn.execute(f"SELECT {other}_synced_at FROM items").fetchone()[0]
    assert conn.execute("SELECT cursor,status,last_synced_at FROM items").fetchone()[0:2] == ("finished", "ok")
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1


def test_optional_replacement_is_per_item_and_daily(conn):
    seed_optional(conn)
    conn.execute("INSERT INTO items(item_id,institution,access_token) VALUES('other','Other',?)", (crypto.encrypt_token("other-fake"),))
    conn.commit()
    seed_optional(conn, "other", "b")
    client = client_for(recurring={"inflow_streams": [stream()], "outflow_streams": []},
                        liabilities={"liabilities": {"credit": [liability()]}})
    sync.sync_items(conn, client, item_id="i")
    assert [r[0] for r in conn.execute("SELECT stream_id FROM recurring_streams ORDER BY stream_id")] == ["old-b", "s"]
    row = conn.execute("SELECT * FROM liabilities WHERE account_id='a'").fetchone()
    assert (row["apr"], row["min_payment"], row["next_due_date"]) == (19.9, 20, "2026-11-01")
    assert conn.execute("SELECT currency FROM recurring_streams WHERE stream_id='s'").fetchone()[0] == "USD"
    assert not conn.execute("SELECT recurring_synced_at FROM items WHERE item_id='other'").fetchone()[0]
    stamp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    conn.execute("UPDATE items SET recurring_synced_at=?,liabilities_synced_at=? WHERE item_id='i'", (stamp, stamp))
    conn.commit()
    refreshed = client_for()
    sync.sync_items(conn, refreshed, item_id="i")
    assert len(refreshed.calls["transactions_recurring_get"]) == 1
    assert len(refreshed.calls["liabilities_get"]) == 1
    assert [r[0] for r in conn.execute("SELECT stream_id FROM recurring_streams")] == ["old-b"]
    assert [r[0] for r in conn.execute("SELECT account_id FROM liabilities")] == ["b"]


@pytest.mark.parametrize("product", ["recurring", "liabilities"])
def test_optional_sql_failure_rolls_back_replacement_only(conn, product):
    seed_optional(conn)
    kwargs = {"recurring": {"inflow_streams": [stream(account_id="missing")], "outflow_streams": []}} if product == "recurring" else {"liabilities": {"liabilities": {"credit": [liability("missing")]}}}
    result = sync.sync_items(conn, client_for(page(added=[tx()]), **kwargs))["items"][0]
    assert result["warnings"] == [{"product": product, "code": "SYNC_FAILED"}]
    table = "recurring_streams" if product == "recurring" else "liabilities"
    assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    assert conn.execute("SELECT cursor FROM items").fetchone()[0] == "finished"


def test_login_required_sanitized_and_other_items_continue(conn):
    conn.execute("INSERT INTO items(item_id,institution,access_token) VALUES('j','Other',?)", (crypto.encrypt_token("other-fake"),))
    conn.commit()
    client = client_for(error("ITEM_LOGIN_REQUIRED"), page())
    client.responses["accounts_get"] = deque([{"accounts": [account("b")]}])
    result = sync.sync_items(conn, client)
    assert result["items"][0] == {"item_id": "i", "status": "login_required", "error": "ITEM_LOGIN_REQUIRED", "action": "fin link --update i"}
    assert result["items"][1]["status"] == "ok"
    assert conn.execute("SELECT status FROM items WHERE item_id='i'").fetchone()[0] == "login_required"
    assert "secret" not in json.dumps(result)
    assert "secret" not in conn.execute("SELECT error FROM sync_log WHERE item_id='i'").fetchone()[0]


@pytest.mark.parametrize("mode", ["status", "type", "code"])
def test_rate_limit_retry_success(conn, sleeps, mode):
    limited = error(status=429) if mode == "status" else error(error_type="RATE_LIMIT_EXCEEDED") if mode == "type" else error("RATE_LIMIT_EXCEEDED")
    client = client_for(limited, limited, page())
    assert sync.sync_items(conn, client)["items"][0]["status"] == "ok"
    assert sleeps == [1, 2]


def test_rate_limit_exhaustion_is_bounded(conn, sleeps):
    limited = error(status=429)
    client = client_for(limited, limited, limited)
    assert sync.sync_items(conn, client)["items"][0]["error"] == "RATE_LIMIT_EXCEEDED"
    assert len(client.calls["transactions_sync"]) == 3 and sleeps == [1, 2]


def test_optional_rate_limit_does_not_rollback_core(conn, sleeps):
    client = client_for(page(added=[tx()]))
    client.responses["transactions_recurring_get"] = deque([error(status=429)] * 3)
    result = sync.sync_items(conn, client)["items"][0]
    assert result["status"] == "ok"
    assert result["warnings"] == [{"product": "recurring", "code": "RATE_LIMIT_EXCEEDED"}]
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    assert sleeps == [1, 2]


@pytest.mark.parametrize("duration", ["6h", "1d", "0.5d"])
def test_staleness_does_not_depend_on_complete_history(conn, duration):
    sync.sync_items(conn, client_for(page(history="INITIAL_UPDATE_COMPLETE")))
    item = conn.execute("SELECT * FROM items").fetchone()
    assert item["last_synced_at"] and item["history_status"] == "INITIAL_UPDATE_COMPLETE"
    client = FakeClient()
    assert sync.sync_items(conn, client, if_stale=duration) == {"items": [{"item_id": "i", "status": "skipped"}]}
    assert not client.calls


@pytest.mark.parametrize("duration", ["1w", "-1h", "h", "6", "1h junk", "nan d"])
def test_invalid_staleness(conn, duration):
    with pytest.raises(ValueError, match="duration"):
        sync.sync_items(conn, FakeClient(), if_stale=duration)


def test_stale_item_syncs_and_error_item_is_not_skipped(conn):
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    conn.execute("UPDATE items SET last_synced_at=?", (old,))
    conn.commit()
    assert sync.sync_items(conn, client_for(), if_stale="6h")["items"][0]["status"] == "ok"
    conn.execute("UPDATE items SET status='login_required'")
    conn.commit()
    assert sync.sync_items(conn, client_for(), if_stale="1d")["items"][0]["status"] == "ok"


def test_initial_sync_omits_null_cursor(conn):
    conn.execute("UPDATE items SET cursor=NULL")
    conn.commit()
    client = client_for()
    sync.sync_items(conn, client)
    assert "cursor" not in client.calls["transactions_sync"][0]


def test_bad_pagination_is_bounded(conn):
    client = client_for(page(cursor="original", more=True))
    assert sync.sync_items(conn, client)["items"][0]["error"] == "SYNC_FAILED"
    assert len(client.calls["transactions_sync"]) == 1


def test_unknown_error_code_and_bad_body_cannot_leak(conn):
    for exc in [error("access-fake-secret"), ApiException(status=500, reason="access-fake-secret")]:
        if not exc.body:
            exc.body = "access-fake-secret"
        result = sync.sync_items(conn, client_for(exc))
        assert result["items"][0]["error"] == "SYNC_FAILED"
        assert "secret" not in json.dumps(result)


def test_live_balance_uses_only_live_endpoint_and_preserves_sync_freshness(conn):
    client = FakeClient(accounts_balance_get=[{"accounts": [account(currency=None)]}])
    result = sync.live_balances(conn, client)
    assert result["items"] == [{"item_id": "i", "status": "ok", "balances": 1}]
    assert result["balances"][0]["currency"] == "BTC"
    assert result["balances"][0]["available"] is None
    assert result["balances"][0]["current"] == 123.45
    assert set(client.calls) == {"accounts_balance_get"}
    assert conn.execute("SELECT cursor,last_synced_at,recurring_synced_at FROM items").fetchone()[:] == ("original", None, None)


def test_live_balance_failure_is_sanitized_and_atomic(conn, sleeps):
    client = FakeClient(accounts_balance_get=[error("ITEM_LOGIN_REQUIRED")])
    result = sync.live_balances(conn, client)
    assert result["items"][0]["status"] == "login_required"
    assert result["balances"] == []
    assert "secret" not in json.dumps(result)
    assert not conn.execute("SELECT count(*) FROM balance_snapshots").fetchone()[0]


def test_sdk_response_models_and_enum_values(conn):
    response = TransactionsSyncResponse(
        accounts=[], added=[], modified=[], removed=[], next_cursor="sdk", has_more=False, request_id="fake",
        transactions_update_status=TransactionsUpdateStatus("HISTORICAL_UPDATE_COMPLETE"))
    sdk_account = AccountBase(
        account_id="a", name="Checking", type=AccountType("depository"),
        mask=None, official_name=None, subtype=None,
        balances=AccountBalance(current=1.0, available=None, limit=None, iso_currency_code="USD", unofficial_currency_code=None))
    client = client_for(response)
    client.responses["accounts_get"] = deque([AccountsGetResponse(accounts=[sdk_account], item={"item_id": "i"}, request_id="fake", _check_type=False)])
    assert sync.sync_items(conn, client)["items"][0]["status"] == "ok"
    assert conn.execute("SELECT type FROM accounts").fetchone()[0] == "depository"


def test_encrypted_tokens_roundtrip_and_tamper_fail_closed(isolated_keyring):
    blob = crypto.encrypt_token("synthetic-token")
    assert isinstance(blob, bytes) and b"synthetic-token" not in blob
    assert crypto.decrypt_token(memoryview(blob)) == "synthetic-token"
    with pytest.raises(RuntimeError, match="could not be decrypted"):
        crypto.decrypt_token(blob[:-1] + b"x")
    isolated_keyring.clear()
    with pytest.raises(RuntimeError, match="could not be decrypted"):
        crypto.decrypt_token(blob)
    assert not isolated_keyring  # Decryption never silently replaces a missing key.


@pytest.mark.parametrize("mode", ["unavailable", "invalid", "not_persisted", "insecure"])
def test_keyring_failures_never_fallback(monkeypatch, isolated_keyring, mode):
    if mode == "unavailable":
        def unavailable(*args):
            raise RuntimeError("secret backend details")
        monkeypatch.setattr(crypto.keyring, "get_password", unavailable)
    elif mode == "invalid":
        isolated_keyring[(crypto._SERVICE, crypto._KEY_NAME)] = "not-a-fernet-key"
    elif mode == "not_persisted":
        monkeypatch.setattr(crypto.keyring, "set_password", lambda *args: None)
    else:
        monkeypatch.setattr(crypto.keyring, "get_keyring", lambda: SimpleNamespace(priority=0))
    with pytest.raises(RuntimeError, match="key is unavailable") as exc:
        crypto.encrypt_token("synthetic-token")
    assert "secret" not in str(exc.value)


def test_decryption_failure_is_sanitized_per_item(conn, isolated_keyring):
    isolated_keyring.clear()
    client = FakeClient()
    result = sync.sync_items(conn, client)
    assert result["items"][0]["error"] == "SYNC_FAILED"
    assert not client.calls


def test_core_product_not_ready_skips_without_advancing_cursor_or_freshness(conn):
    result = sync.sync_items(conn, client_for(error("PRODUCT_NOT_READY")))["items"][0]
    assert result == {"item_id": "i", "status": "skipped", "error": "PRODUCT_NOT_READY"}
    assert conn.execute("SELECT cursor,last_synced_at,status FROM items").fetchone()[:] == ("original", None, "ok")
    assert sync.sync_items(conn, client_for(), if_stale="6h")["items"][0]["status"] == "ok"


@pytest.mark.parametrize("product", ["recurring", "liabilities"])
def test_malformed_optional_response_does_not_clear_existing_data(conn, product):
    seed_optional(conn)
    result = sync.sync_items(conn, client_for(**{product: {}}))["items"][0]
    assert result["warnings"] == [{"product": product, "code": "SYNC_FAILED"}]
    table = "recurring_streams" if product == "recurring" else "liabilities"
    assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1


def test_accounts_cannot_be_reassigned_between_items(conn):
    conn.execute("INSERT INTO items(item_id,institution,access_token) VALUES('j','Other',?)", (crypto.encrypt_token("other-fake"),))
    conn.execute("INSERT INTO accounts(account_id,item_id) VALUES('a','j')")
    conn.commit()
    result = sync.sync_items(conn, client_for(page(added=[tx()])), item_id="i")["items"][0]
    assert result["error"] == "SYNC_FAILED"
    assert conn.execute("SELECT item_id FROM accounts WHERE account_id='a'").fetchone()[0] == "j"
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_concurrent_newer_cursor_is_not_overwritten(conn):
    path = conn.execute("PRAGMA database_list").fetchone()["file"]

    def concurrent_commit(request):
        other = connect(path)
        with other:
            other.execute("UPDATE items SET cursor='newer-committed',last_synced_at='2026-10-22T00:00:00+00:00'")
        other.close()
        return {"accounts": [account()]}

    client = client_for(page(added=[tx("obsolete")]))
    client.responses["accounts_get"] = deque([concurrent_commit])
    result = sync.sync_items(conn, client)["items"][0]
    assert result["error"] == "SYNC_CONFLICT"
    assert conn.execute("SELECT cursor,status,last_synced_at FROM items").fetchone()[:] == (
        "newer-committed", "ok", "2026-10-22T00:00:00+00:00")
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0


@pytest.mark.parametrize("environment", ["sandbox", "production"])
def test_build_real_sdk_client_without_calls(environment):
    config = SimpleNamespace(client_id="fake-client", secret="fake-secret", environment=environment)
    client = build_client(config)
    assert isinstance(client, PlaidApi)
    assert client.api_client.configuration.host == f"https://{environment}.plaid.com"
    assert not client.api_client.configuration.debug
    client.api_client.close()


def test_build_client_validates_configuration():
    with pytest.raises(ValueError, match="environment"):
        build_client(SimpleNamespace(environment="unknown", client_id="fake", secret="fake"))
    with pytest.raises(ValueError, match="required"):
        build_client(SimpleNamespace(environment="sandbox", client_id="", secret=""))


def test_savepoints_preserve_caller_transaction(conn):
    conn.execute("UPDATE items SET institution='Caller change'")
    result = sync.sync_items(conn, client_for(page(added=[tx()])))
    assert result["items"][0]["status"] == "ok"
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT institution,cursor FROM items").fetchone()[:] == ("Test Bank", "original")
    assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
