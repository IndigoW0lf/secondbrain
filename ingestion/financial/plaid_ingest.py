"""
ingestion/financial/plaid_ingest.py

Pulls transactions and account balances from all linked Plaid accounts.
Run on a schedule (daily recommended) via scheduler/jobs.py.

FIRST-TIME SETUP:
  1. Create a Plaid account at https://dashboard.plaid.com (free sandbox)
  2. For production, complete Plaid's item-add flow to get access tokens.
     The easiest way: use Plaid Link (their hosted UI) once per institution.
     See: https://plaid.com/docs/link/
  3. Add your access tokens to config/secrets.env as:
       PLAID_ACCESS_TOKEN_CHASE=access-production-xxxx
       PLAID_ACCESS_TOKEN_AMEX=access-production-yyyy
"""

import os
from datetime import date, datetime, timedelta, timezone

import plaid
from dotenv import load_dotenv
from plaid.api import plaid_api
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
from rich.console import Console
from rich.table import Table

load_dotenv("config/secrets.env")

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from storage.store import get_db, log_ingest_finish, log_ingest_start

console = Console()


def get_plaid_client() -> plaid_api.PlaidApi:
    env_name = os.getenv("PLAID_ENV", "sandbox").lower()
    env_map = {
        "sandbox": plaid.Environment.Sandbox,
        "development": plaid.Environment.Sandbox,
        "production": plaid.Environment.Production,
    }
    configuration = plaid.Configuration(
        host=env_map[env_name],
        api_key={
            "clientId": os.getenv("PLAID_CLIENT_ID"),
            "secret": os.getenv("PLAID_SECRET"),
        }
    )
    api_client = plaid.ApiClient(configuration)
    return plaid_api.PlaidApi(api_client)


def get_access_tokens() -> dict[str, str]:
    tokens = {}
    for key, val in os.environ.items():
        if key.startswith("PLAID_ACCESS_TOKEN_") and val:
            institution = key.replace("PLAID_ACCESS_TOKEN_", "").lower()
            tokens[institution] = val
    return tokens


def sync_accounts(client: plaid_api.PlaidApi, access_token: str, institution: str) -> list[str]:
    request = AccountsGetRequest(access_token=access_token)
    response = client.accounts_get(request)
    account_ids = []

    with get_db() as conn:
        for acct in response.accounts:
            account_id = acct.account_id
            account_ids.append(account_id)
            conn.execute("""
                INSERT INTO accounts
                    (id, institution, name, type, subtype, mask,
                     current_balance, available_balance, last_synced)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    current_balance=excluded.current_balance,
                    available_balance=excluded.available_balance,
                    last_synced=excluded.last_synced
            """, (
                account_id,
                institution,
                acct.name,
                str(acct.type),
                str(acct.subtype) if acct.subtype else None,
                acct.mask,
                acct.balances.current,
                acct.balances.available,
                datetime.now(timezone.utc).isoformat()
            ))

    console.print(f"  [green]Synced {len(account_ids)} accounts[/] from {institution}")
    return account_ids


def sync_transactions(
    client: plaid_api.PlaidApi,
    access_token: str,
    institution: str,
    days_back: int = 90
) -> tuple[int, int]:
    start_date = date.today() - timedelta(days=days_back)
    end_date = date.today()

    request = TransactionsGetRequest(
        access_token=access_token,
        start_date=start_date,
        end_date=end_date,
        options=TransactionsGetRequestOptions(
            count=500,
            include_personal_finance_category=True,
        )
    )

    all_transactions = []
    response = client.transactions_get(request)
    all_transactions.extend(response.transactions)
    total = response.total_transactions

    while len(all_transactions) < total:
        request.options.offset = len(all_transactions)
        response = client.transactions_get(request)
        all_transactions.extend(response.transactions)

    added = updated = 0

    with get_db() as conn:
        for txn in all_transactions:
            category = None
            subcategory = None
            if txn.personal_finance_category:
                category = txn.personal_finance_category.primary
                subcategory = txn.personal_finance_category.detailed

            if not category and txn.category:
                category = txn.category[0] if txn.category else None
                subcategory = txn.category[1] if len(txn.category) > 1 else None

            existing = conn.execute(
                "SELECT id FROM transactions WHERE id=?", (txn.transaction_id,)
            ).fetchone()

            conn.execute("""
                INSERT INTO transactions
                    (id, account_id, date, amount, description, merchant_name,
                     category, subcategory, pending, location_city, location_state,
                     payment_channel, logo_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    pending=excluded.pending,
                    amount=excluded.amount,
                    merchant_name=excluded.merchant_name,
                    category=excluded.category,
                    subcategory=excluded.subcategory
            """, (
                txn.transaction_id,
                txn.account_id,
                str(txn.date),
                txn.amount,
                txn.name,
                txn.merchant_name,
                category,
                subcategory,
                1 if txn.pending else 0,
                txn.location.city if txn.location else None,
                txn.location.region if txn.location else None,
                txn.payment_channel,
                txn.logo_url,
            ))

            if existing:
                updated += 1
            else:
                added += 1

    return added, updated


def run(days_back: int = 90):
    """Run the full financial ingest. Called by scheduler or directly."""
    log_id = log_ingest_start("plaid_financial")
    total_added = total_updated = 0

    console.print("\n[bold]Financial ingest starting...[/]")

    tokens = get_access_tokens()
    if not tokens:
        msg = (
            "No Plaid access tokens found in config/secrets.env.\n"
            "Add them as: PLAID_ACCESS_TOKEN_YOURBANK=access-production-xxxx\n"
            "See README for first-time Plaid setup instructions."
        )
        console.print(f"[yellow]⚠ {msg}[/]")
        log_ingest_finish(log_id, 0, 0, error=msg)
        return

    client = get_plaid_client()

    for institution, token in tokens.items():
        console.print(f"\n[bold blue]{institution}[/]")
        try:
            sync_accounts(client, token, institution)
            added, updated = sync_transactions(client, token, institution, days_back)
            total_added += added
            total_updated += updated
            console.print(f"  Transactions: [green]+{added} new[/], [yellow]{updated} updated[/]")
        except Exception as e:
            console.print(f"  [red]Error: {e}[/]")
            log_ingest_finish(log_id, total_added, total_updated, error=str(e))
            raise

    table = Table(title="Accounts & Balances")
    table.add_column("Institution")
    table.add_column("Account")
    table.add_column("Type")
    table.add_column("Balance", justify="right")

    with get_db() as conn:
        rows = conn.execute("""
            SELECT institution, name, subtype, current_balance
            FROM accounts ORDER BY institution, name
        """).fetchall()
        for row in rows:
            balance = f"${row['current_balance']:,.2f}" if row['current_balance'] else "—"
            table.add_row(row['institution'], row['name'], row['subtype'] or "", balance)

    console.print(table)
    console.print(f"\n[bold green]Done.[/] {total_added} new transactions, {total_updated} updated.")
    log_ingest_finish(log_id, total_added, total_updated)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90, help="Days of history to pull")
    args = parser.parse_args()
    run(days_back=args.days)
