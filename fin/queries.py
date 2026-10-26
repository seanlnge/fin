"""Local-only financial queries.

Money is serialized as decimal strings rounded to cents (ROUND_HALF_UP).
Transaction amounts are negative for outflow; balance/debt values keep native
signs. All ranges are rolling, inclusive dates, not complete calendar periods.
Transaction minimum/maximum filters compare absolute, unrounded magnitudes.
"""

import calendar
import csv
import json
import os
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


_TX_COLUMNS = """t.transaction_id, t.account_id, a.name AS account_name,
    a.mask, t.date, t.authorized_date, t.amount, t.currency,
    t.merchant_name, t.name, t.category, t.category_detail,
    t.pending, t.pending_transaction_id"""
_TX_FIELDS = [field.strip().split()[-1].split(".")[-1]
              for field in _TX_COLUMNS.split(",")]
_TRANSFERS = {"TRANSFER_IN", "TRANSFER_OUT",
              "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"}
_INCOME = {"INCOME", "EARNED_INCOME", "UNEARNED_INCOME"}


def _rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Amount must be a finite number") from None
    if not result.is_finite():
        raise ValueError("Amount must be a finite number")
    return result


def _money(value):
    if value is None:
        return None
    value = _decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(abs(value) if value == 0 else value, ".2f")


def _date(value, anchor):
    if isinstance(value, date):
        return date.fromisoformat(value.isoformat())
    match = re.fullmatch(r"(\d+)(d|w|mo|y)", str(value))
    if not match:
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            raise ValueError("Date must be ISO YYYY-MM-DD or relative d/w/mo/y") from None
    count, unit = int(match[1]), match[2]
    if unit in {"d", "w"}:
        return anchor - timedelta(days=count * (7 if unit == "w" else 1))
    month = anchor.year * 12 + anchor.month - 1 - count * (12 if unit == "y" else 1)
    year, month = divmod(month, 12)
    month += 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def _range(since, until):
    today = date.today()
    end = _date(until, today) if until is not None else today
    start = _date(since, end)
    if start > end:
        raise ValueError("since must not be after until")
    return {"since": start.isoformat(), "until": end.isoformat(),
            "inclusive": True, "mode": "rolling"}


def _result(conn, data, *, range=None, truncated=False, product=None, **metadata):
    freshness = _rows(conn, """SELECT item_id, institution, status,
        last_synced_at, history_status, recurring_synced_at, liabilities_synced_at
        FROM items ORDER BY item_id""")
    return {"data": data, "range": range, "freshness": {
        "source": "local_cache", "product": product, "items": freshness},
        "truncated": truncated,
        "amount_convention": "negative outflow, positive inflow; balances and liabilities retain native signs",
        "money_format": "decimal strings rounded to cents, ROUND_HALF_UP",
        **metadata}


def _limit(limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    return limit


def _account(conn, value):
    matches = _rows(conn, "SELECT account_id, name, official_name, mask FROM accounts")
    exact = [row for row in matches if row["account_id"] == value]
    if exact:
        return exact[0]["account_id"]
    value = str(value).casefold()
    matches = [row for row in matches if any(
        field is not None and value == str(field).casefold()
        for field in (row["name"], row["official_name"], row["mask"]))]
    if len(matches) != 1:
        raise ValueError("Account filter is ambiguous" if matches else "Account not found")
    return matches[0]["account_id"]


def _transactions(conn, range=None, account=None, merchant=None, category=None,
                  include_pending=True, text=None):
    clauses, params = [], []
    if range:
        clauses.append("t.date BETWEEN ? AND ?")
        params.extend((range["since"], range["until"]))
    if account is not None:
        clauses.append("t.account_id = ?")
        params.append(_account(conn, account))
    if merchant is not None:
        clauses.append("instr(lower(coalesce(t.merchant_name, '')), lower(?)) > 0")
        params.append(merchant)
    if category is not None:
        clauses.append("(upper(t.category) = upper(?) OR upper(t.category_detail) = upper(?))")
        params.extend((category, category))
    if not include_pending:
        clauses.append("coalesce(t.pending, 0) = 0")
    if text is not None:
        clauses.append("""(instr(lower(coalesce(t.merchant_name, '')), lower(?)) > 0
            OR instr(lower(coalesce(t.name, '')), lower(?)) > 0)""")
        params.extend((text, text))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return _rows(conn, "SELECT " + _TX_COLUMNS + " FROM transactions t LEFT JOIN accounts a"
                 " ON a.account_id = t.account_id" + where +
                 " ORDER BY t.date DESC, t.transaction_id", params)


def _public_tx(row):
    return {**row, "amount": _money(-_decimal(row["amount"])) if row["amount"] is not None else None,
            "pending": bool(row["pending"])}


def status(conn):
    return _result(conn, _rows(conn, """SELECT i.item_id, i.institution, i.status,
        i.last_synced_at, i.history_status, i.recurring_synced_at,
        i.liabilities_synced_at, count(a.account_id) AS account_count
        FROM items i LEFT JOIN accounts a ON a.item_id = i.item_id
        GROUP BY i.item_id ORDER BY i.institution, i.item_id"""))


def accounts(conn):
    rows = _rows(conn, """SELECT a.account_id, a.item_id, a.name, a.official_name,
        a.type, a.subtype, a.mask, b.as_of, b.current, b.available,
        b.credit_limit, b.currency FROM accounts a LEFT JOIN balance_snapshots b
        ON b.account_id = a.account_id AND b.as_of = (
            SELECT max(s.as_of) FROM balance_snapshots s WHERE s.account_id = a.account_id)
        ORDER BY a.name, a.account_id""")
    for row in rows:
        for field in ("current", "available", "credit_limit"):
            row[field] = _money(row[field])
    return _result(conn, rows, product="balances")


def balances(conn, history=None):
    if history is None:
        return accounts(conn)
    range = _range(history, None)
    rows = _rows(conn, """SELECT b.account_id, a.name AS account_name,
        b.as_of, b.current, b.available, b.credit_limit, b.currency
        FROM balance_snapshots b LEFT JOIN accounts a ON a.account_id = b.account_id
        WHERE substr(b.as_of, 1, 10) BETWEEN ? AND ?
        AND b.as_of = (SELECT max(s.as_of) FROM balance_snapshots s
            WHERE s.account_id = b.account_id
            AND substr(s.as_of, 1, 10) = substr(b.as_of, 1, 10))
        ORDER BY b.as_of, b.account_id""", (range["since"], range["until"]))
    for row in rows:
        row["date"] = row["as_of"][:10]
        for field in ("current", "available", "credit_limit"):
            row[field] = _money(row[field])
    return _result(conn, rows, range=range, product="balances",
                   series="last observed snapshot per account per day; no forward filling")


def tx(conn, since='30d', until=None, account=None, merchant=None, category=None,
       minimum=None, maximum=None, limit=200, include_pending=True):
    limit = _limit(limit)
    low = _decimal(minimum) if minimum is not None else None
    high = _decimal(maximum) if maximum is not None else None
    if (low is not None and low < 0) or (high is not None and high < 0):
        raise ValueError("minimum and maximum must be nonnegative absolute magnitudes")
    if low is not None and high is not None and low > high:
        raise ValueError("minimum must not exceed maximum")
    range = _range(since, until)
    rows = _transactions(conn, range, account, merchant, category, include_pending)
    rows = [row for row in rows if (low is None or (row["amount"] is not None and
            abs(_decimal(row["amount"])) >= low)) and (high is None or
            (row["amount"] is not None and abs(_decimal(row["amount"])) <= high))]
    return _result(conn, [_public_tx(row) for row in rows[:limit]], range=range,
                   truncated=len(rows) > limit, minimum_maximum="absolute magnitude",
                   include_pending=include_pending)


def search(conn, text, limit=200):
    limit = _limit(limit)
    rows = _transactions(conn, text=text)
    return _result(conn, [_public_tx(row) for row in rows[:limit]],
                   truncated=len(rows) > limit)


def _period(value, period):
    day = date.fromisoformat(value)
    if period == "day":
        return day.isoformat()
    if period == "week":
        return (day - timedelta(days=day.weekday())).isoformat()
    if period == "month":
        return day.replace(day=1).isoformat()
    return day.replace(month=1, day=1).isoformat()


def _aggregate(conn, by, period, since, until, include_transfers, include_pending):
    if period not in {"day", "week", "month", "year"}:
        raise ValueError("period must be day, week, month, or year")
    if by is not None and by not in {"category", "merchant", "account"}:
        raise ValueError("by must be category, merchant, or account")
    range = _range(since, until)
    groups = {}
    for row in _transactions(conn, range, include_pending=include_pending):
        if not include_transfers and (
                row["category"] in _TRANSFERS or row["category_detail"] in _TRANSFERS):
            continue
        if row["amount"] is None or (by is not None and row["category"] in _INCOME):
            continue
        group = None
        if by == "category":
            group = row["category"] or "Uncategorized"
        elif by == "merchant":
            group = row["merchant_name"] or row["name"] or "Unknown"
        elif by == "account":
            group = row["account_id"]
        key = (_period(row["date"], period), row["currency"], group)
        bucket = groups.setdefault(key, {"inflow": Decimal(0), "outflow": Decimal(0), "count": 0})
        amount = -_decimal(row["amount"])
        bucket["inflow" if amount > 0 else "outflow"] += amount
        bucket["count"] += 1
    data = []
    for (start, currency, group), bucket in sorted(groups.items(), key=lambda pair: tuple(
            "" if value is None else str(value) for value in pair[0])):
        net = bucket["inflow"] + bucket["outflow"]
        row = {"period": start, "currency": currency, "count": bucket["count"]}
        if by is not None:
            row.update({by: group, "amount": _money(net)})
        else:
            row.update({"inflow": _money(bucket["inflow"]),
                        "outflow": _money(bucket["outflow"]), "net": _money(net)})
        data.append(row)
    return _result(conn, data, range=range, period=period, by=by,
                   include_pending=include_pending, include_transfers=include_transfers,
                   transfer_policy="category-based heuristic, not proof of own-account transfers",
                   spend_policy="refunds net against spending; INCOME, EARNED_INCOME, and UNEARNED_INCOME categories excluded" if by else None)


def spend(conn, by='category', period='month', since='6mo', until=None,
          include_transfers=False, include_pending=False):
    if by not in {"category", "merchant", "account"}:
        raise ValueError("by must be category, merchant, or account")
    return _aggregate(conn, by, period, since, until, include_transfers, include_pending)


def cashflow(conn, period='month', since='6mo', until=None,
             include_transfers=False, include_pending=False):
    return _aggregate(conn, None, period, since, until, include_transfers, include_pending)


def recurring(conn, active=False):
    rows = _rows(conn, """SELECT r.stream_id, r.account_id, a.name AS account_name,
        r.direction, r.merchant, r.frequency, r.avg_amount, r.last_amount,
        r.last_date, r.is_active, r.currency FROM recurring_streams r
        LEFT JOIN accounts a ON a.account_id = r.account_id""" +
        (" WHERE r.is_active = 1" if active else "") + " ORDER BY r.merchant, r.stream_id")
    for row in rows:
        row["is_active"] = bool(row["is_active"])
        for field in ("avg_amount", "last_amount"):
            if row[field] is not None:
                amount = abs(_decimal(row[field]))
                row[field] = _money(-amount if row["direction"] == "outflow" else amount)
    return _result(conn, rows, product="recurring")


def liabilities(conn):
    rows = _rows(conn, """SELECT l.account_id, a.name AS account_name, l.apr,
        l.min_payment, l.next_due_date, l.last_statement_balance
        FROM liabilities l LEFT JOIN accounts a ON a.account_id = l.account_id
        ORDER BY l.next_due_date, l.account_id""")
    for row in rows:
        for field in ("min_payment", "last_statement_balance"):
            row[field] = _money(row[field])
    return _result(conn, rows, product="liabilities")


def export(conn, since='2y', format='csv', out=None):
    if format not in {"csv", "json"}:
        raise ValueError("format must be csv or json")
    range = _range(since, None)
    path = Path(out).expanduser() if out is not None else Path.home() / ".fin" / ("transactions." + format)
    target = path.resolve()
    for database in _rows(conn, "PRAGMA database_list"):
        if not database["file"]:
            continue
        database_path = Path(database["file"])
        for base in (database_path, database_path.resolve()):
            for suffix in ("", "-wal", "-shm", "-journal"):
                if target == Path(str(base) + suffix).resolve():
                    raise FileExistsError(
                        f"Refusing to export to SQLite database or sidecar: {path}. "
                        "Choose a different --out path.")
    rows = [_public_tx(row) for row in _transactions(conn, range)]
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Exclusive creation prevents clobbering files or following existing symlinks;
    # the creation mode keeps financial data private from the first write on POSIX.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        raise FileExistsError(
            f"Export destination already exists: {path}. "
            "Choose a new --out path or move the existing file first.") from None
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        if format == "json":
            json.dump(rows, handle, indent=2, allow_nan=False)
        else:
            writer = csv.DictWriter(handle, fieldnames=_TX_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    return _result(conn, {"path": str(path), "format": format, "count": len(rows)},
                   range=range, include_pending=True)
