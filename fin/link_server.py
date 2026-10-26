"""Browser-based Hosted Link: no public server or inbound webhook required."""
import sys
import time
import webbrowser

from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_hosted_link import LinkTokenCreateHostedLink
from plaid.model.link_token_transactions import LinkTokenTransactions
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_get_request import LinkTokenGetRequest
from plaid.model.products import Products

from fin.crypto import decrypt_token, encrypt_token


def import_item(conn, client, token: str) -> dict:
    """Import an existing access token without creating a new Plaid Item."""
    encrypted = encrypt_token(token)
    payload = client.item_get(ItemGetRequest(access_token=token))
    item = payload["item"]
    institution_id = item["institution_id"]
    if conn.execute("SELECT 1 FROM items WHERE item_id=? OR institution_id=?",
                    (item["item_id"], institution_id)).fetchone():
        raise ValueError("Item or institution already imported")
    institution = client.institutions_get_by_id(InstitutionsGetByIdRequest(
        institution_id=institution_id, country_codes=[CountryCode("US"), CountryCode("CA")],
    ))["institution"]
    with conn:
        conn.execute("INSERT INTO items(item_id,institution,institution_id,access_token) VALUES(?,?,?,?)",
                     (item["item_id"], institution["name"], institution_id, encrypted))
    return {"item_id": item["item_id"], "institution": institution["name"], "status": "imported",
            "warning": "Existing Transactions history window is preserved; import cannot extend it to 730 days."}


def finish_link(conn, client, result: dict) -> dict:
    institution = result.get("institution") or {}
    institution_id = institution.get("institution_id")
    if not institution_id:
        raise ValueError("Link did not return an institution ID; token was not exchanged")
    with conn:
        # Serialize duplicate checking and exchange across separate CLI processes.
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM items WHERE institution_id = ?", (institution_id,)).fetchone():
            raise ValueError("Institution already linked; use fin link --update ITEM instead")
        # Verify the keychain before consuming a permanent production Item slot.
        encrypt_token("keychain-check")
        response = client.item_public_token_exchange(ItemPublicTokenExchangeRequest(public_token=result["public_token"]))
        item_id = response["item_id"]
        conn.execute(
            "INSERT INTO items(item_id,institution,institution_id,access_token) VALUES(?,?,?,?)",
            (item_id, institution.get("name") or institution_id, institution_id,
             encrypt_token(response["access_token"])),
        )
    return {"item_id": item_id, "institution": institution.get("name"), "status": "linked"}


def link(conn, client, *, update=None, timeout=900) -> dict:
    kwargs = dict(
        client_name="fin local", language="en", country_codes=[CountryCode("US"), CountryCode("CA")],
        user=LinkTokenCreateRequestUser(client_user_id="fin-local-owner"),
        hosted_link=LinkTokenCreateHostedLink(),
    )
    if update:
        item = conn.execute("SELECT access_token FROM items WHERE item_id=?", (update,)).fetchone()
        if not item:
            raise ValueError("Unknown Item")
        kwargs["access_token"] = decrypt_token(item["access_token"])
        kwargs["additional_consented_products"] = [Products("liabilities")]
    else:
        encrypt_token("keychain-check")
        existing = conn.execute("SELECT institution,item_id FROM items ORDER BY institution").fetchall()
        for item in existing:
            print(f"Already linked: {item['institution']}. Use fin link --update {item['item_id']} instead of linking it again.", file=sys.stderr)
        kwargs.update(
            products=[Products("transactions")],
            optional_products=[Products("liabilities")],
            transactions=LinkTokenTransactions(days_requested=730),
        )
    response = client.link_token_create(LinkTokenCreateRequest(**kwargs))
    url = response["hosted_link_url"]
    if not webbrowser.open(url):
        raise RuntimeError("Could not open the browser; run fin link from an interactive desktop")
    print("Complete Plaid Link in the browser. Waiting up to 15 minutes; Ctrl+C cancels.", file=sys.stderr)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.link_token_get(LinkTokenGetRequest(link_token=response["link_token"]))
        payload = payload.to_dict() if hasattr(payload, "to_dict") else payload
        sessions = payload.get("link_sessions") or []
        for session in sessions:
            if update:
                # Modern Hosted Link sessions may omit the deprecated on_success.
                # HANDOFF is Plaid's stable event for a successfully completed flow.
                handoff = any(event.get("event_name") == "HANDOFF" for event in session.get("events") or [])
                if session.get("on_success") is not None or handoff:
                    with conn:
                        conn.execute("UPDATE items SET status='ok',last_synced_at=NULL,liabilities_synced_at=NULL WHERE item_id=?", (update,))
                    return {"item_id": update, "status": "reauthenticated",
                            "confirmation": "HANDOFF" if handoff else "on_success"}
            else:
                results = (session.get("results") or {}).get("item_add_results") or []
                if results:
                    return finish_link(conn, client, results[0])
        # finished_at can arrive before the completion metadata. Only an explicit
        # exit is a cancellation; allow active/resumed sessions to finish first.
        if sessions and all(session.get("finished_at") for session in sessions):
            latest = max(enumerate(sessions), key=lambda entry: (str(entry[1].get("started_at") or ""), entry[0]))[1]
            exited = (
                latest.get("exit") is not None or latest.get("on_exit") is not None
                or any(event.get("event_name") == "EXIT" for event in latest.get("events") or [])
            )
            if exited:
                raise ValueError("Link exited without confirmed completion. Permissions may already have changed; run fin sync to check.")
        time.sleep(3)
    if update:
        raise TimeoutError("Timed out waiting for Link completion metadata. Permissions may already have changed; run fin sync to check.")
    raise TimeoutError("Link timed out; no token was exchanged")
