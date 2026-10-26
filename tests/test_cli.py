import json
import subprocess
from datetime import date

import pytest
from typer.testing import CliRunner

from fin.cli import app
from fin.db import connect


def test_permission_failure_is_specific_and_sanitized(runner, monkeypatch):
    from fin import db
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["private-command"], stderr="private-details")
    monkeypatch.setattr(db, "connect", fail)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "PERMISSION_SETUP_FAILED"
    assert "private-command" not in result.stdout
    assert "private-details" not in result.stdout


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("FIN_DB_PATH", str(tmp_path / "fin.db"))
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    return CliRunner()


def test_empty_status_without_credentials(runner):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == []


@pytest.mark.parametrize("args", [["tx", "--limit", "wrong"], ["unknown"], ["tx", "--wat"], ["sql"]])
def test_usage_errors_are_json(runner, args):
    result = runner.invoke(app, args)
    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


def test_no_database_is_json_error(runner):
    result = runner.invoke(app, ["accounts"])
    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


def test_sql_denied_is_json_error(runner, monkeypatch, tmp_path):
    conn = connect(tmp_path / "fin.db")
    conn.close()
    result = runner.invoke(app, ["sql", "SELECT access_token FROM items"])
    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] == "DATABASE_ERROR"


def test_help(runner):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "cashflow" in result.stdout


def test_partial_sync_failure_has_nonzero_exit(runner, monkeypatch):
    from fin import plaid_client, sync
    monkeypatch.setattr(plaid_client, "build_client", lambda config: object())
    monkeypatch.setattr(sync, "sync_items", lambda *a, **kw: {
        "items": [{"item_id": "i", "status": "login_required", "error": "ITEM_LOGIN_REQUIRED"}]})
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "SYNC_FAILED"


@pytest.mark.parametrize("args", [
    ["accounts"], ["balances"], ["balances", "--history", "90d"], ["tx"],
    ["tx", "--min", "1", "--max", "20", "--no-include-pending"],
    ["search", "Coffee"], ["spend", "--by", "merchant"], ["cashflow"],
    ["recurring", "--active"], ["liabilities"], ["sql", "SELECT amount FROM transactions"],
])
def test_query_commands_return_json(runner, tmp_path, args):
    conn = connect(tmp_path / "fin.db")
    today = date.today().isoformat()
    with conn:
        conn.execute("INSERT INTO items(item_id,institution,access_token,history_status) VALUES('i','Bank',X'00','HISTORICAL_UPDATE_COMPLETE')")
        conn.execute("INSERT INTO accounts(account_id,item_id,name,type) VALUES('a','i','Checking','depository')")
        conn.execute("INSERT INTO transactions(transaction_id,account_id,date,amount,currency,name,category,pending) VALUES('t','a',?,4.25,'USD','Coffee','FOOD_AND_DRINK',0)", (today,))
        conn.execute("INSERT INTO balance_snapshots(account_id,as_of,current,currency) VALUES('a',?,100,'USD')", (today,))
    conn.close()
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.stdout
    assert isinstance(json.loads(result.stdout)["data"], list)


def test_unknown_sync_item_is_an_error(runner, monkeypatch):
    from fin import plaid_client
    monkeypatch.setattr(plaid_client, "build_client", lambda config: object())
    result = runner.invoke(app, ["sync", "--item", "missing"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"
