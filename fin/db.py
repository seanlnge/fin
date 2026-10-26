"""SQLite connections. Query connections cannot read credential columns."""
import os
import sqlite3
import subprocess
import csv
from pathlib import Path


def _private(path: Path, *, directory: bool = False):
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    # Git Bash puts Unix utilities first on PATH; use the Windows ACL tools.
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    identity = subprocess.run([str(system32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                              capture_output=True, text=True, check=True)
    sid = next(csv.reader([identity.stdout.strip()]))[1]
    permission = "(OI)(CI)F" if directory else "F"
    subprocess.run([str(system32 / "icacls.exe"), str(path), "/inheritance:r", "/grant:r",
                    f"*{sid}:{permission}", f"*S-1-5-18:{permission}"],
                   capture_output=True, check=True)


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
    else:
        new_directory = not path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        if new_directory or path.parent.resolve() == (Path.home() / ".fin").resolve():
            _private(path.parent, directory=True)
        conn = sqlite3.connect(path, timeout=30)
        try:
            _private(path)
            conn.executescript(Path(__file__).with_name("schema.sql").read_text())
        except BaseException:
            conn.close()
            raise
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def safe_sql(conn: sqlite3.Connection, query: str) -> dict:
    if not query.lstrip().upper().startswith(("SELECT ", "SELECT\n", "WITH ", "WITH\n")):
        raise ValueError("Only a single SELECT query is allowed")
    tables = {"accounts", "balance_snapshots", "transactions", "recurring_streams", "liabilities", "items", "sync_log"}

    def authorize(action, arg1, arg2, database, source):
        if action == sqlite3.SQLITE_READ:
            if arg1 not in tables or (arg1 == "items" and arg2 in {"access_token", "cursor"}):
                return sqlite3.SQLITE_DENY
        elif action == sqlite3.SQLITE_FUNCTION:
            if (arg2 or "").lower() in {"load_extension", "readfile", "writefile"}:
                return sqlite3.SQLITE_DENY
        elif action not in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    ticks = 0

    def budget():
        nonlocal ticks
        ticks += 1
        return ticks > 1000

    conn.set_authorizer(authorize)
    conn.set_progress_handler(budget, 1000)
    try:
        rows = conn.execute(query).fetchmany(1001)
        return {"data": [dict(row) for row in rows[:1000]], "truncated": len(rows) > 1000,
                "amount_convention": "Raw database amounts use Plaid signs: positive = outflow"}
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
