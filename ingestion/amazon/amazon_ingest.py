"""
ingestion/amazon/amazon_ingest.py

Parse Amazon order confirmation emails already in the emails table.
Filters: from_address LIKE '%amazon.com%'. Stores rows in amazon_orders.
"""

import os
import sys
import re
import json
from typing import Any

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track

load_dotenv("config/secrets.env")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from storage.store import get_db, log_ingest_start, log_ingest_finish

console = Console()

ORDER_ID_RE = re.compile(r"\b(\d{3}-\d{7}-\d{7})\b")
TOTAL_RE = re.compile(
    r"(?:Grand\s+Total|Order\s+Total|Total\s+for\s+this\s+(?:order|shipment)|"
    r"Total\s*due|Amount\s*to\s*pay|Total\s*charged|Total\s*:\s*|"
    r"Payment\s*total)\s*[:.]?\s*(?:USD\s*)?\$?\s*([\d,]+\.\d{2})",
    re.I,
)
PRICE_LINE_RE = re.compile(r"^\s*\$?\s*([\d,]+\.\d{2})\s*$")


def _parse_items_from_text(text: str) -> list[dict[str, Any]]:
    """Heuristic extraction of line items from Amazon email plain text."""
    items: list[dict[str, Any]] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if re.search(r"Qty|Quantity", ln, re.I) and i + 1 < len(lines):
            name = lines[i + 1]
            if len(name) > 3 and not PRICE_LINE_RE.match(name):
                price = None
                if i + 2 < len(lines):
                    pm = PRICE_LINE_RE.match(lines[i + 2])
                    if pm:
                        try:
                            price = float(pm.group(1).replace(",", ""))
                        except ValueError:
                            pass
                items.append({"name": name[:500], "qty": 1, "price": price})
    if not items:
        # Fallback: lines that look like titles near a price
        for ln in lines:
            if 10 < len(ln) < 200 and not ln.startswith("http"):
                if any(x in ln.lower() for x in ("sold by", "condition:", "order #")):
                    continue
                if ORDER_ID_RE.search(ln):
                    continue
                if TOTAL_RE.search(ln):
                    continue
                items.append({"name": ln[:500], "qty": 1, "price": None})
            if len(items) >= 15:
                break
    return items[:25]


def parse_amazon_email(body: str, subject: str) -> dict | None:
    """Return dict with order id, total, items, status or None if not parseable."""
    soup = BeautifulSoup(body or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    combined = f"{subject}\n{text}"

    om = ORDER_ID_RE.search(combined)
    if not om:
        return None
    order_id = om.group(1)

    total = None
    tm = TOTAL_RE.search(combined)
    if tm:
        try:
            total = float(tm.group(1).replace(",", ""))
        except ValueError:
            pass

    items = _parse_items_from_text(text)
    status = None
    for phrase in ("Shipped", "Delivered", "Arriving", "Preparing", "Cancelled"):
        if phrase.lower() in text.lower():
            status = phrase
            break

    return {
        "id": order_id,
        "total_amount": total,
        "currency": "USD",
        "status": status,
        "items": items,
        "shipping_address": None,
    }


def run():
    log_id = log_ingest_start("amazon_orders")
    console.print("\n[bold]Amazon order email parse starting...[/]")

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, subject, body_text, date, from_address
            FROM emails
            WHERE from_address LIKE '%amazon.com%'
              AND (body_text IS NOT NULL AND LENGTH(body_text) > 20)
            """
        ).fetchall()

    if not rows:
        console.print("[yellow]No Amazon-looking emails found.[/]")
        log_ingest_finish(log_id, 0, 0)
        return

    console.print(f"  Candidate emails: {len(rows)}")

    touched: set[str] = set()
    for row in track(rows, description="Parsing orders..."):
        rowd = dict(row)
        parsed = parse_amazon_email(rowd.get("body_text") or "", rowd.get("subject") or "")
        if not parsed:
            continue

        order_date = rowd.get("date") or ""
        if order_date and "T" in order_date:
            order_date = order_date[:10]
        elif order_date and len(order_date) >= 10:
            order_date = order_date[:10]

        items_json = json.dumps(parsed["items"], ensure_ascii=False)

        with get_db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO amazon_orders
                    (id, order_date, total_amount, currency, status, items,
                     shipping_address, source_email_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parsed["id"],
                    order_date,
                    parsed["total_amount"],
                    parsed["currency"],
                    parsed["status"],
                    items_json,
                    parsed["shipping_address"],
                    rowd["id"],
                ),
            )
            touched.add(parsed["id"])

    console.print(f"[bold green]Done.[/] {len(touched)} Amazon order(s) upserted from email.")
    log_ingest_finish(log_id, len(touched), 0)


if __name__ == "__main__":
    run()
