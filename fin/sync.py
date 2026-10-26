"""Offline-store ingestion. Network responses are buffered before SQLite writes.

Public results contain per-item status/counts and fixed, sanitized error codes.
Optional-product warnings never invalidate a successful transaction sync.
"""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import re
import time

from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.transactions_recurring_get_request import TransactionsRecurringGetRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from .crypto import decrypt_token

_KNOWN_CODES = {
    "ITEM_LOGIN_REQUIRED", "PRODUCT_NOT_READY", "PRODUCT_NOT_SUPPORTED",
    "PRODUCTS_NOT_SUPPORTED", "PRODUCT_NOT_ENABLED", "INVALID_PRODUCT",
    "NO_ACCOUNTS", "NO_LIABILITY_ACCOUNTS", "INSTITUTION_NOT_SUPPORTED", "ADDITIONAL_CONSENT_REQUIRED",
    "ACCESS_NOT_GRANTED", "RATE_LIMIT_EXCEEDED", "INSTITUTION_DOWN",
    "INSTITUTION_NOT_RESPONDING", "INVALID_ACCESS_TOKEN", "ITEM_NOT_SUPPORTED",
    "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION",
}
_MUTATION = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"


class _CursorChanged(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def _error(exc):
    if isinstance(exc, _CursorChanged):
        return "SYNC_CONFLICT"
    # Never stringify SDK exceptions: their body/headers can contain credentials.
    try:
        body = json.loads(getattr(exc, "body", None) or "{}")
        if not isinstance(body, dict):
            body = {}
    except (TypeError, ValueError):
        body = {}
    if getattr(exc, "status", None) == 429 or body.get("error_type") == "RATE_LIMIT_EXCEEDED":
        return "RATE_LIMIT_EXCEEDED"
    code = body.get("error_code")
    return code if isinstance(code, str) and code in _KNOWN_CODES else "SYNC_FAILED"


def _call(method, request):
    for attempt in range(3):
        try:
            response = method(request)
            return response.to_dict() if hasattr(response, "to_dict") else response
        except Exception as exc:
            if _error(exc) != "RATE_LIMIT_EXCEEDED" or attempt == 2:
                raise
            time.sleep(2 ** attempt)


@contextmanager
def _atomic(conn):
    # SAVEPOINT also preserves a caller-owned outer transaction, if present.
    conn.execute("SAVEPOINT fin_ingestion")
    try:
        yield
        conn.execute("RELEASE SAVEPOINT fin_ingestion")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT fin_ingestion")
        conn.execute("RELEASE SAVEPOINT fin_ingestion")
        raise


def _fresh(value, duration, now):
    if not value:
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return timedelta(0) <= now - stamp < duration
    except (ValueError, TypeError):
        return False


def _duration(value):
    if value is None:
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([hd])", value)
    if not match:
        raise ValueError("if_stale must be a duration such as 6h or 1d")
    return timedelta(hours=float(match[1]) * (24 if match[2] == "d" else 1))


def _json(value):
    return json.dumps(value, default=lambda obj: obj.isoformat() if isinstance(obj, (date, datetime)) else str(obj))


def _currency(value):
    return value.get("iso_currency_code") or value.get("unofficial_currency_code")


def _accounts(conn, item_id, accounts, stamp):
    balances = []
    for account in accounts:
        account_id = account["account_id"]
        owner = conn.execute("SELECT item_id FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if owner and owner["item_id"] != item_id:
            raise ValueError("Account ownership mismatch")
        conn.execute(
            """INSERT INTO accounts(account_id,item_id,name,official_name,type,subtype,mask)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET
               name=excluded.name,official_name=excluded.official_name,type=excluded.type,
               subtype=excluded.subtype,mask=excluded.mask""",
            (account_id, item_id, account.get("name"), account.get("official_name"),
             account.get("type"), account.get("subtype"), account.get("mask")),
        )
        b = account.get("balances") or {}
        row = {"account_id": account_id, "item_id": item_id, "as_of": stamp,
               "current": b.get("current"), "available": b.get("available"),
               "credit_limit": b.get("limit"), "currency": _currency(b)}
        conn.execute(
            """INSERT INTO balance_snapshots(account_id,as_of,current,available,credit_limit,currency)
               VALUES(:account_id,:as_of,:current,:available,:credit_limit,:currency)""", row,
        )
        balances.append(row)
    return balances


def _pages(client, token, cursor):
    for restart in range(3):
        pages = []
        next_cursor = cursor
        seen = {cursor}
        try:
            for _ in range(1000):
                args = {"access_token": token, "count": 500}
                if next_cursor is not None:
                    args["cursor"] = next_cursor
                page = _call(client.transactions_sync, TransactionsSyncRequest(**args))
                pages.append(page)
                next_cursor = page["next_cursor"]
                if not page["has_more"]:
                    return pages, next_cursor
                if next_cursor in seen:
                    raise ValueError("Nonadvancing pagination cursor")
                seen.add(next_cursor)
            raise ValueError("Pagination limit exceeded")
        except Exception as exc:
            if _error(exc) != _MUTATION or restart == 2:
                raise
            time.sleep(2 ** restart)


def _transactions(conn, item_id, pages):
    counts = {"added": 0, "modified": 0, "removed": 0}
    pending = set()
    for page in pages:
        for kind in ("added", "modified"):
            for tx in page.get(kind, []):
                owner = conn.execute("SELECT item_id FROM accounts WHERE account_id=?", (tx["account_id"],)).fetchone()
                if not owner or owner["item_id"] != item_id:
                    raise ValueError("Unknown transaction account")
                existing = conn.execute(
                    "SELECT a.item_id FROM transactions t JOIN accounts a USING(account_id) WHERE transaction_id=?",
                    (tx["transaction_id"],),
                ).fetchone()
                if existing and existing["item_id"] != item_id:
                    raise ValueError("Transaction ownership mismatch")
                category = tx.get("personal_finance_category") or {}
                conn.execute(
                    """INSERT INTO transactions(transaction_id,account_id,date,authorized_date,amount,currency,
                       merchant_name,name,category,category_detail,pending,pending_transaction_id,raw_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(transaction_id) DO UPDATE SET
                       account_id=excluded.account_id,date=excluded.date,authorized_date=excluded.authorized_date,
                       amount=excluded.amount,currency=excluded.currency,merchant_name=excluded.merchant_name,
                       name=excluded.name,category=excluded.category,category_detail=excluded.category_detail,
                       pending=excluded.pending,pending_transaction_id=excluded.pending_transaction_id,
                       raw_json=excluded.raw_json""",
                    (tx["transaction_id"], tx["account_id"], str(tx["date"]),
                     str(tx["authorized_date"]) if tx.get("authorized_date") else None,
                     tx["amount"], _currency(tx), tx.get("merchant_name"), tx.get("name"),
                     category.get("primary"), category.get("detailed"), int(tx.get("pending", False)),
                     tx.get("pending_transaction_id"), _json(tx)),
                )
                if not tx.get("pending") and tx.get("pending_transaction_id"):
                    pending.add(tx["pending_transaction_id"])
                counts[kind] += 1
        for tx in page.get("removed", []):
            conn.execute(
                "DELETE FROM transactions WHERE transaction_id=? AND account_id IN (SELECT account_id FROM accounts WHERE item_id=?)",
                (tx["transaction_id"], item_id),
            )
            counts["removed"] += 1
    # Run after all pages so even a pending addition later in the batch is settled.
    for transaction_id in pending:
        conn.execute(
            "DELETE FROM transactions WHERE transaction_id=? AND pending=1 AND account_id IN (SELECT account_id FROM accounts WHERE item_id=?)",
            (transaction_id, item_id),
        )
    return counts


def _optional(conn, client, item, token, now):
    warnings = []
    for product in ("recurring", "liabilities"):
        column = product + "_synced_at"
        if _fresh(item[column], timedelta(days=1), now):
            continue
        try:
            if product == "recurring":
                response = _call(client.transactions_recurring_get, TransactionsRecurringGetRequest(access_token=token))
            else:
                response = _call(client.liabilities_get, LiabilitiesGetRequest(access_token=token))
            with _atomic(conn):
                # Liabilities may return additional loan/credit accounts.
                if response.get("accounts"):
                    _accounts(conn, item["item_id"], response["accounts"], _now().isoformat())
                table = "recurring_streams" if product == "recurring" else "liabilities"
                conn.execute(f"DELETE FROM {table} WHERE account_id IN (SELECT account_id FROM accounts WHERE item_id=?)", (item["item_id"],))
                if product == "recurring":
                    for direction in ("inflow", "outflow"):
                        for stream in response[direction + "_streams"]:
                            _owned_account(conn, item["item_id"], stream["account_id"])
                            avg = stream.get("average_amount") or {}
                            last = stream.get("last_amount") or {}
                            conn.execute(
                                "INSERT INTO recurring_streams VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (stream["stream_id"], stream["account_id"], direction,
                                 stream.get("merchant_name") or stream.get("description"), stream.get("frequency"),
                                 avg.get("amount"), last.get("amount"),
                                 str(stream["last_date"]) if stream.get("last_date") else None,
                                 int(stream.get("is_active", False)), _currency(avg) or _currency(last)),
                            )
                else:
                    for group in (response["liabilities"] or {}).values():
                        for liability in group or []:
                            _owned_account(conn, item["item_id"], liability["account_id"])
                            aprs = liability.get("aprs") or []
                            apr = next((a.get("apr_percentage") for a in aprs if a.get("apr_type") == "purchase_apr"), None)
                            if apr is None:
                                apr = liability.get("interest_rate", {}).get("percentage") if liability.get("interest_rate") else None
                            conn.execute(
                                "INSERT INTO liabilities VALUES(?,?,?,?,?,?)",
                                (liability["account_id"], apr, liability.get("minimum_payment_amount"),
                                 str(liability["next_payment_due_date"]) if liability.get("next_payment_due_date") else None,
                                 liability.get("last_statement_balance"), _json(liability)),
                            )
                conn.execute(f"UPDATE items SET {column}=? WHERE item_id=?", (now.isoformat(), item["item_id"]))
        except Exception as exc:
            warnings.append({"product": product, "code": _error(exc)})
    return warnings


def _owned_account(conn, item_id, account_id):
    row = conn.execute("SELECT item_id FROM accounts WHERE account_id=?", (account_id,)).fetchone()
    if not row or row["item_id"] != item_id:
        raise ValueError("Unknown optional-product account")


def _failure(conn, item_id, stamp, exc):
    code = _error(exc)
    status = "login_required" if code == "ITEM_LOGIN_REQUIRED" else "skipped" if code == "PRODUCT_NOT_READY" else "error"
    with _atomic(conn):
        if status != "skipped" and code != "SYNC_CONFLICT":
            conn.execute("UPDATE items SET status=? WHERE item_id=?", (status, item_id))
        conn.execute("INSERT INTO sync_log(item_id,started_at,error) VALUES(?,?,?)", (item_id, stamp, code))
    result = {"item_id": item_id, "status": status, "error": code}
    if status == "login_required":
        result["action"] = f"fin link --update {item_id}"
    return result


def sync_items(conn, client, *, item_id=None, if_stale=None) -> dict:
    """Return {items: [{item_id, status, counts..., history_status, warnings}]}.

    Skips report status='skipped'; failures report a fixed error code. A successful
    sync advances freshness even while Plaid is still loading historical data.
    """
    duration = _duration(if_stale)
    rows = conn.execute("SELECT * FROM items" + (" WHERE item_id=?" if item_id is not None else "") + " ORDER BY item_id",
                        (item_id,) if item_id is not None else ()).fetchall()
    if item_id is not None and not rows:
        raise ValueError("Unknown Item")
    results = []
    for item in rows:
        now = _now()
        stamp = now.isoformat()
        if duration is not None and item["status"] == "ok" and _fresh(item["last_synced_at"], duration, now):
            results.append({"item_id": item["item_id"], "status": "skipped"})
            continue
        try:
            token = decrypt_token(item["access_token"])
            pages, cursor = _pages(client, token, item["cursor"])
            accounts = _call(client.accounts_get, AccountsGetRequest(access_token=token))
            history = pages[-1].get("transactions_update_status")
            with _atomic(conn):
                # This read acquires SQLite's transaction lock before any writes.
                # A second writer either changed the cursor already, or cannot
                # commit between this check and our cursor update.
                current = conn.execute("SELECT cursor FROM items WHERE item_id=?", (item["item_id"],)).fetchone()
                if current is None or current["cursor"] != item["cursor"]:
                    raise _CursorChanged()
                balances = _accounts(conn, item["item_id"], accounts["accounts"], stamp)
                counts = _transactions(conn, item["item_id"], pages)
                conn.execute(
                    "UPDATE items SET cursor=?,status='ok',last_synced_at=?,history_status=COALESCE(?,history_status) WHERE item_id=?",
                    (cursor, stamp, history, item["item_id"]),
                )
                conn.execute("INSERT INTO sync_log(item_id,started_at,added,modified,removed) VALUES(?,?,?,?,?)",
                             (item["item_id"], stamp, counts["added"], counts["modified"], counts["removed"]))
        except Exception as exc:
            results.append(_failure(conn, item["item_id"], stamp, exc))
            continue
        warnings = _optional(conn, client, item, token, now)
        results.append({"item_id": item["item_id"], "status": "ok", **counts,
                        "balances": len(balances), "history_status": history or item["history_status"],
                        "warnings": warnings})
    return {"items": results}


def live_balances(conn, client) -> dict:
    """Append real-time snapshots; never advance transaction/optional freshness.

    Returns {items: [per-item status], balances: [account balance records]}.
    """
    results, balances = [], []
    for item in conn.execute("SELECT * FROM items ORDER BY item_id").fetchall():
        stamp = _now().isoformat()
        try:
            token = decrypt_token(item["access_token"])
            response = _call(client.accounts_balance_get, AccountsBalanceGetRequest(access_token=token))
            with _atomic(conn):
                current = _accounts(conn, item["item_id"], response["accounts"], stamp)
            balances.extend(current)
            results.append({"item_id": item["item_id"], "status": "ok", "balances": len(current)})
        except Exception as exc:
            results.append(_failure(conn, item["item_id"], stamp, exc))
    return {"items": results, "balances": balances}
