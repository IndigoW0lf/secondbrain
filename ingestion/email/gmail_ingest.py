"""
ingestion/email/gmail_ingest.py

Pulls emails from Gmail, stores metadata in SQLite,
and embeds body text into ChromaDB for semantic search.

FIRST-TIME SETUP:
  1. Go to https://console.cloud.google.com
  2. Create a project → Enable Gmail API (and Calendar/Drive if using those ingests)
  3. Create OAuth 2.0 credentials (Desktop app type)
  4. Download JSON → save as config/google_credentials.json
  5. Run this script once — it will open a browser to authenticate.
     Token saved to config/google_token.json for future runs.

SCOPES:
  - gmail.readonly, calendar.readonly, drive.readonly (shared via ingestion.google_common)
"""

import os
import sys
import json
import base64
from email import message_from_bytes
from typing import Optional

import html2text
from googleapiclient.discovery import build
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track

load_dotenv("config/secrets.env")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from ingestion.google_common import get_google_credentials
from storage.store import get_db, upsert_to_chroma, log_ingest_start, log_ingest_finish

console = Console()

BATCH_SIZE = 200


def get_gmail_service():
    creds = get_google_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def decode_body(payload) -> str:
    """Recursively extract plain text from a Gmail message payload."""
    h = html2text.HTML2Text()
    h.ignore_links = True
    h.ignore_images = True

    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    if payload.get("mimeType") == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            return h.handle(html)

    parts = payload.get("parts", [])
    for part in parts:
        text = decode_body(part)
        if text:
            return text
    return ""


def get_header(headers: list, name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def run(max_emails: int = BATCH_SIZE, query: str = ""):
    """
    Pull recent emails from Gmail.
    query: Gmail search query, e.g. "after:2024/01/01" or "from:amazon"
    """
    log_id = log_ingest_start("gmail")
    console.print("\n[bold]Gmail ingest starting...[/]")

    service = get_gmail_service()
    added = updated = 0

    params = {"userId": "me", "maxResults": max_emails}
    if query:
        params["q"] = query

    result = service.users().messages().list(**params).execute()
    message_refs = result.get("messages", [])

    if not message_refs:
        console.print("[yellow]No messages found.[/]")
        log_ingest_finish(log_id, 0, 0)
        return

    console.print(f"Processing {len(message_refs)} emails...")

    with get_db() as conn:
        existing_ids = set(
            row[0] for row in conn.execute("SELECT id FROM emails").fetchall()
        )

    new_refs = [r for r in message_refs if r["id"] not in existing_ids]
    console.print(f"  {len(new_refs)} new, {len(message_refs) - len(new_refs)} already stored")

    embed_ids = []
    embed_texts_list = []
    embed_metas = []

    for ref in track(new_refs, description="Fetching emails..."):
        try:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="full"
            ).execute()

            headers = msg["payload"].get("headers", [])
            date_str = get_header(headers, "Date")
            subject = get_header(headers, "Subject")
            from_raw = get_header(headers, "From")
            to_raw = get_header(headers, "To")

            from_name, from_address = "", from_raw
            if "<" in from_raw:
                parts = from_raw.split("<")
                from_name = parts[0].strip().strip('"')
                from_address = parts[1].rstrip(">").strip()

            body = decode_body(msg["payload"])
            body_stored = body[:8000] if body else ""

            labels = json.dumps(msg.get("labelIds", []))
            has_attachment = any(
                p.get("filename") for p in msg["payload"].get("parts", [])
            )

            with get_db() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO emails
                        (id, thread_id, from_address, from_name, to_address,
                         subject, date, snippet, labels, has_attachment, body_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ref["id"],
                        msg.get("threadId"),
                        from_address,
                        from_name,
                        to_raw,
                        subject,
                        date_str,
                        msg.get("snippet", ""),
                        labels,
                        1 if has_attachment else 0,
                        body_stored,
                    ),
                )

            added += 1

            embed_text = (
                f"Subject: {subject}\nFrom: {from_name} <{from_address}>\n\n"
                f"{msg.get('snippet', '')}"
            )
            embed_ids.append(ref["id"])
            embed_texts_list.append(embed_text)
            embed_metas.append({
                "source": "gmail",
                "date": date_str[:10] if date_str else "",
                "from": from_address,
                "subject": subject,
            })

        except Exception as e:
            console.print(f"  [red]Error on {ref['id']}: {e}[/]")
            continue

    if embed_ids:
        console.print(f"Embedding {len(embed_ids)} emails...")
        upsert_to_chroma("emails", embed_ids, embed_texts_list, embed_metas)

        with get_db() as conn:
            conn.executemany(
                "UPDATE emails SET embedded=1 WHERE id=?",
                [(eid,) for eid in embed_ids],
            )

    console.print(f"[bold green]Done.[/] {added} new emails stored and embedded.")
    log_ingest_finish(log_id, added, updated)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=200, help="Max emails to fetch")
    parser.add_argument("--query", type=str, default="", help="Gmail search query")
    args = parser.parse_args()
    run(max_emails=args.max, query=args.query)
