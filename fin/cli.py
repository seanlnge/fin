"""JSON-first commands for local financial data."""
import getpass
import json
import sqlite3
import subprocess
from contextlib import closing
from typing import Annotated

import typer
from typer.core import TyperGroup
from typer._click.exceptions import ClickException

from fin import db, queries
from fin.config import load_config


def emit(value):
    typer.echo(json.dumps(value, default=str, allow_nan=False))


def emit_sync_result(value):
    failed = any(item.get("status") in {"error", "login_required"} for item in value.get("items", []))
    if failed:
        value = {**value, "error": "One or more Items could not be refreshed", "code": "SYNC_FAILED"}
    emit(value)
    if failed:
        raise SystemExit(1)


class JsonGroup(TyperGroup):
    def main(self, *args, **kwargs):
        kwargs["standalone_mode"] = False
        try:
            return super().main(*args, **kwargs)
        except ClickException as exc:
            emit({"error": exc.format_message(), "code": "INVALID_ARGUMENT"})
            raise SystemExit(exc.exit_code)
        except (ValueError, FileNotFoundError) as exc:
            emit({"error": str(exc), "code": "INVALID_ARGUMENT"})
            raise SystemExit(1)
        except sqlite3.Error:
            emit({"error": "Database query failed or access was denied", "code": "DATABASE_ERROR"})
            raise SystemExit(1)
        except subprocess.CalledProcessError:
            emit({"error": "Could not secure the local database with Windows permissions", "code": "PERMISSION_SETUP_FAILED"})
            raise SystemExit(1)
        except KeyboardInterrupt:
            emit({"error": "Command cancelled", "code": "CANCELLED"})
            raise SystemExit(130)
        except Exception:
            # SDK and keychain exception strings can contain credentials or request bodies.
            emit({"error": "Operation failed; check configuration, keychain, and Plaid connectivity", "code": "OPERATION_FAILED"})
            raise SystemExit(1)


app = typer.Typer(cls=JsonGroup, no_args_is_help=True, add_completion=False,
                  help="Local bank data. Commands return JSON; help is human-readable.")


def read_query(function, **kwargs):
    config = load_config()
    if not config.db_path.exists():
        raise ValueError("No local database yet. Run fin sync or fin link first")
    with closing(db.connect(config.db_path, readonly=True)) as conn:
        emit(function(conn, **kwargs))


@app.command()
def status():
    """Items, account counts, and freshness."""
    config = load_config()
    if not config.db_path.exists():
        emit({"data": [], "message": "No banks linked. Run fin link to get started."})
    else:
        read_query(queries.status)


@app.command()
def accounts():
    """Accounts and their latest cached balances."""
    read_query(queries.accounts)


@app.command()
def balances(history: str | None = None, live: bool = False):
    """Native account balances; positive card balances represent debt."""
    if live:
        if history:
            raise ValueError("--live cannot be combined with --history")
        from fin.plaid_client import build_client
        from fin.sync import live_balances
        config = load_config()
        with closing(db.connect(config.db_path)) as conn:
            emit_sync_result(live_balances(conn, build_client(config)))
    else:
        read_query(queries.balances, history=history)


@app.command()
def tx(since: str = "30d", until: str | None = None, account: str | None = None,
       merchant: str | None = None, category: str | None = None,
       minimum: Annotated[float | None, typer.Option("--min", help="Minimum absolute amount")] = None,
       maximum: Annotated[float | None, typer.Option("--max", help="Maximum absolute amount")] = None,
       limit: int = 200, include_pending: bool = True):
    """Transactions; negative amounts are money out."""
    read_query(queries.tx, since=since, until=until, account=account, merchant=merchant,
               category=category, minimum=minimum, maximum=maximum, limit=limit,
               include_pending=include_pending)


@app.command()
def search(text: str, limit: int = 200):
    """Literal substring search over merchant and transaction names."""
    read_query(queries.search, text=text, limit=limit)


@app.command()
def spend(by: str = "category", period: str = "month", since: str = "6mo", until: str | None = None,
          include_transfers: bool = False, include_pending: bool = False):
    """Net spending, grouped by currency and period, with refunds offset."""
    read_query(queries.spend, by=by, period=period, since=since, until=until,
               include_transfers=include_transfers, include_pending=include_pending)


@app.command()
def cashflow(period: str = "month", since: str = "6mo", until: str | None = None,
             include_transfers: bool = False, include_pending: bool = False):
    """Income and expenses across linked accounts, not checking-only cash flow."""
    read_query(queries.cashflow, period=period, since=since, until=until,
               include_transfers=include_transfers, include_pending=include_pending)


@app.command()
def recurring(active: bool = False):
    """Recurring inflow and outflow streams from Plaid."""
    read_query(queries.recurring, active=active)


@app.command()
def liabilities():
    """Credit and loan information where supported."""
    read_query(queries.liabilities)


@app.command()
def sql(query: str):
    """One read-only SELECT, with credential protection and a 1,000-row cap."""
    read_query(db.safe_sql, query=query)


@app.command("export")
def export_command(since: str = "2y", format: str = "csv", out: str | None = None):
    """Export all matching transactions to a local CSV or JSON file."""
    read_query(queries.export, since=since, format=format, out=out)


@app.command()
def sync(item: str | None = None, if_stale: str | None = None):
    """Sync bank data; no bank writes. Requires Plaid credentials."""
    from fin.plaid_client import build_client
    from fin.sync import sync_items
    config = load_config()
    with closing(db.connect(config.db_path)) as conn:
        result = sync_items(conn, build_client(config), item_id=item, if_stale=if_stale)
        emit_sync_result(result)


@app.command()
def link(update: str | None = None):
    """Open browser for a new bank or repair an existing Item."""
    from fin.link_server import link as run_link
    from fin.plaid_client import build_client
    config = load_config()
    with closing(db.connect(config.db_path)) as conn:
        emit(run_link(conn, build_client(config), update=update))


@app.command("import-item")
def import_item():
    """Import an existing quickstart token via a hidden prompt (no new Item slot)."""
    from fin.link_server import import_item as run_import
    from fin.plaid_client import build_client
    config = load_config()
    token = getpass.getpass("Existing Plaid access token (hidden): ").strip()
    if not token.startswith("access-" + config.environment + "-"):
        raise ValueError("Token must match the configured PLAID_ENV")
    with closing(db.connect(config.db_path)) as conn:
        emit(run_import(conn, build_client(config), token))


if __name__ == "__main__":
    app()
