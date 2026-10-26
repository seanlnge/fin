"""Opt-in real Plaid Sandbox smoke test; never creates Production Items.

Run with FIN_RUN_SANDBOX_TESTS=1 and PLAID_ENV=sandbox after configuring credentials.
"""
import os

import pytest


@pytest.mark.skipif(os.getenv("FIN_RUN_SANDBOX_TESTS") != "1", reason="Live Sandbox test is opt-in")
def test_sandbox_sync(tmp_path):
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
    from plaid.model.products import Products
    from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
    from plaid.model.item_remove_request import ItemRemoveRequest

    from fin.config import load_config
    from fin.crypto import encrypt_token
    from fin.db import connect
    from fin.plaid_client import build_client
    from fin.queries import balances, tx
    from fin.sync import sync_items

    config = load_config()
    assert config.environment == "sandbox", "Refusing integration tests outside Sandbox"
    client = build_client(config)
    public = client.sandbox_public_token_create(SandboxPublicTokenCreateRequest(
        institution_id="ins_109508", initial_products=[Products("transactions")]))
    item = client.item_public_token_exchange(ItemPublicTokenExchangeRequest(public_token=public["public_token"]))
    try:
        conn = connect(tmp_path / "sandbox.db")
        try:
            with conn:
                conn.execute("INSERT INTO items(item_id,institution,institution_id,access_token) VALUES(?,?,?,?)",
                             (item["item_id"], "Sandbox Bank", "ins_109508", encrypt_token(item["access_token"])))
            result = sync_items(conn, client)
            assert result["items"][0]["status"] in {"ok", "skipped"}
            assert isinstance(tx(conn)["data"], list)
            assert isinstance(balances(conn)["data"], list)
        finally:
            conn.close()
    finally:
        client.item_remove(ItemRemoveRequest(access_token=item["access_token"]))
