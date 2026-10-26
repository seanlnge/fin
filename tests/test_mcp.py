import asyncio
import json
import sys

import pytest

pytest.importorskip("fastmcp")
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from fin.config import Config
from fin.db import connect
from fin.mcp_server import create_server


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "fin.db"
    conn = connect(path)
    with conn:
        conn.execute("INSERT INTO items(item_id,institution,access_token,cursor) VALUES('i','Fixture Bank',X'736563726574','secret-cursor')")
        conn.execute("INSERT INTO accounts(account_id,item_id,name,type) VALUES('a','i','Checking','depository')")
        conn.execute("INSERT INTO transactions(transaction_id,account_id,date,amount,currency,name,category,pending) VALUES('t','a','2026-10-01',4.25,'USD','Coffee','FOOD_AND_DRINK',0)")
    conn.close()
    return Config(path)


def test_tool_discovery_and_safety_annotations(config):
    async def check():
        async with Client(create_server(config)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert len(tools) == 12
            assert not {"fin_link", "fin_import_item", "fin_export"} & tools.keys()
            assert tools["fin_status"].annotations.read_only_hint is True
            assert tools["fin_sync"].annotations.read_only_hint is False
            assert tools["fin_live_balances"].annotations.open_world_hint is True
            assert tools["fin_tx"].input_schema["properties"]["limit"]["maximum"] == 1000
            result = await client.call_tool("fin_status")
            assert result.data["data"][0]["institution"] == "Fixture Bank"
            assert "secret-cursor" not in json.dumps(result.data)
            resources = await client.list_resources()
            assert "fin://schema" in {str(resource.uri) for resource in resources}
            content = await client.read_resource("fin://schema")
            assert "CREATE TABLE" in content[0].text
    asyncio.run(check())


@pytest.mark.parametrize("tool,args", [
    ("fin_accounts", {}), ("fin_balances", {}),
    ("fin_tx", {"since": "2026-01-01", "until": "2026-12-31"}),
    ("fin_search", {"text": "Coffee"}),
    ("fin_spend", {"since": "2026-01-01", "until": "2026-12-31"}),
    ("fin_cashflow", {"since": "2026-01-01", "until": "2026-12-31"}),
    ("fin_recurring", {}), ("fin_liabilities", {}),
    ("fin_sql", {"query": "SELECT name FROM transactions"}),
])
def test_queries_return_structured_json(config, tool, args):
    async def check():
        async with Client(create_server(config)) as client:
            result = await client.call_tool(tool, args)
            assert isinstance(result.data["data"], list)
    asyncio.run(check())


@pytest.mark.parametrize("query", ["SELECT access_token FROM items", "DELETE FROM transactions", "SELECT cursor FROM items"])
def test_sql_protection_is_a_tool_error(config, query):
    async def check():
        async with Client(create_server(config)) as client:
            result = await client.call_tool("fin_sql", {"query": query}, raise_on_error=False)
            assert result.is_error
            assert "secret-cursor" not in str(result.content)
            assert "736563726574" not in str(result.content)
    asyncio.run(check())


def test_missing_database_does_not_create_files(tmp_path):
    path = tmp_path / "absent.db"
    async def check():
        async with Client(create_server(Config(path))) as client:
            assert (await client.call_tool("fin_status")).data["data"] == []
            assert (await client.call_tool("fin_sync", raise_on_error=False)).is_error
            assert (await client.call_tool("fin_balances", raise_on_error=False)).is_error
    asyncio.run(check())
    assert not path.exists()


def test_sync_failures_are_sanitized(config, monkeypatch):
    from fin import plaid_client
    def fail(config):
        raise RuntimeError("access-production-PRIVATE PLAID_SECRET=PRIVATE")
    monkeypatch.setattr(plaid_client, "build_client", fail)
    async def check():
        async with Client(create_server(config)) as client:
            result = await client.call_tool("fin_sync", raise_on_error=False)
            assert result.is_error
            assert "PRIVATE" not in str(result.content)
    asyncio.run(check())


def test_sync_default_and_partial_failure(config, monkeypatch):
    from fin import plaid_client, sync
    calls = []
    monkeypatch.setattr(plaid_client, "build_client", lambda config: object())
    def fake(conn, client, **kwargs):
        calls.append(kwargs)
        return {"items": [{"item_id": "i", "status": "login_required", "action": "fin link --update i"}]}
    monkeypatch.setattr(sync, "sync_items", fake)
    async def check():
        async with Client(create_server(config)) as client:
            result = await client.call_tool("fin_sync", raise_on_error=False)
            assert result.is_error
            assert "login_required" in str(result.content)
    asyncio.run(check())
    assert calls == [{"item_id": None, "if_stale": "6h"}]


def test_stdio_handshake_from_unrelated_directory(config, tmp_path):
    # Desktop does not launch with the project as its working directory.
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    transport = StdioTransport(command=sys.executable,
        args=["-m", "fin.mcp_server", "--env-file", str(env_file)],
        cwd=str(tmp_path), env={"FIN_DB_PATH": str(config.db_path), "PLAID_ENV": "sandbox"},
        keep_alive=False)
    async def check():
        async with Client(transport, timeout=30) as client:
            assert len(await client.list_tools()) == 12
            result = await client.call_tool("fin_status")
            assert result.data["data"][0]["institution"] == "Fixture Bank"
    asyncio.run(check())
