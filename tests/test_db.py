import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from fin.db import connect, safe_sql
from fin import db


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL executable resolution")
def test_permission_tools_use_system32_not_path(tmp_path, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout='"test-user","S-1-5-21-1234"\n')
    monkeypatch.setattr(db.subprocess, "run", run)
    db._private(tmp_path / "fin.db")
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    assert calls[0][0] == str(system32 / "whoami.exe")
    assert calls[1][0] == str(system32 / "icacls.exe")


@pytest.mark.skipif(os.name != "nt", reason="Git Bash PATH regression on Windows")
def test_database_setup_with_git_unix_tools_first(tmp_path, monkeypatch):
    git_bin = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "usr" / "bin"
    if not (git_bin / "whoami.exe").exists():
        pytest.skip("Git Bash is not installed")
    monkeypatch.setenv("PATH", str(git_bin) + os.pathsep + os.environ.get("PATH", ""))
    conn = connect(tmp_path / "fin.db")
    assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    conn.close()


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "fin.db"
    with connect(path) as conn:
        conn.execute("INSERT INTO items(item_id,institution,access_token,cursor) VALUES('i','Bank',X'736563726574','cursor')")
    return path


def test_readonly_and_credential_denial(database):
    conn = connect(database, readonly=True)
    assert safe_sql(conn, "SELECT institution FROM items")["data"] == [{"institution": "Bank"}]
    for query in ("SELECT access_token FROM items", "SELECT * FROM items", "SELECT cursor FROM items",
                  "SELECT hex(access_token) FROM items", "SELECT sql FROM sqlite_master"):
        with pytest.raises(sqlite3.DatabaseError):
            safe_sql(conn, query)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM items")
    conn.close()


def test_sql_single_statement_and_row_limit(database):
    conn = connect(database, readonly=True)
    with pytest.raises(ValueError):
        safe_sql(conn, "DELETE FROM items")
    with pytest.raises(sqlite3.ProgrammingError):
        safe_sql(conn, "SELECT 1; SELECT 2")
    result = safe_sql(conn, "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1002) SELECT x FROM n")
    assert len(result["data"]) == 1000
    assert result["truncated"] is True
    conn.close()


def test_sql_execution_budget(database):
    conn = connect(database, readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        safe_sql(conn, "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n")
    assert safe_sql(conn, "SELECT 1 AS ok")["data"] == [{"ok": 1}]
    conn.close()
