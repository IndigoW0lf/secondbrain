"""
ingestion/notes/apple_notes_ingest.py

Ingest Apple Notes exports saved as HTML files from a folder.
Set APPLE_NOTES_EXPORT_PATH in config/secrets.env to the folder path.

Stores rows in documents with source=apple_notes and embeds into collection "documents".
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track

load_dotenv("config/secrets.env")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from storage.store import get_db, upsert_to_chroma, log_ingest_start, log_ingest_finish, stable_id

console = Console()


def parse_apple_notes_html(html: str, fallback_title: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else fallback_title
    body = soup.get_text("\n", strip=True)
    if h1 and body.startswith(title):
        body = body[len(title) :].lstrip()
    return title, body


def run(export_dir: str | None = None):
    log_id = log_ingest_start("apple_notes")
    export_dir = export_dir or os.getenv("APPLE_NOTES_EXPORT_PATH")
    if not export_dir:
        console.print(
            "[red]Set APPLE_NOTES_EXPORT_PATH in config/secrets.env "
            "to your exported HTML folder.[/]"
        )
        log_ingest_finish(log_id, 0, 0, error="APPLE_NOTES_EXPORT_PATH not set")
        return

    root = Path(export_dir).expanduser()
    if not root.is_dir():
        console.print(f"[red]Not a directory: {root}[/]")
        log_ingest_finish(log_id, 0, 0, error="invalid export path")
        return

    console.print(f"\n[bold]Apple Notes ingest from {root}[/]")

    paths = sorted(root.rglob("*.html")) + sorted(root.rglob("*.htm"))
    if not paths:
        console.print("[yellow]No HTML files found.[/]")
        log_ingest_finish(log_id, 0, 0)
        return

    console.print(f"  Found {len(paths)} HTML files")

    embed_ids, embed_texts, embed_metas = [], [], []
    added = 0

    for path in track(paths, description="Importing notes..."):
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            console.print(f"  [yellow]Skip {path}: {e}[/]")
            continue

        title, body = parse_apple_notes_html(raw, path.stem)
        body = body.strip()
        if not body:
            continue

        doc_id = stable_id("apple_notes", str(path.resolve()))
        wc = len(body.split())
        mtime = datetime_iso(path)

        with get_db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO documents
                    (id, source, title, path, mime_type, created_at, modified_at,
                     word_count, body_text, embedded)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    doc_id,
                    "apple_notes",
                    title[:2000] or path.stem,
                    str(path.resolve()),
                    "text/html",
                    mtime,
                    mtime,
                    wc,
                    body[:500000],
                ),
            )
        added += 1

        embed_body = body[:12000] if len(body) > 12000 else body
        embed_ids.append(doc_id)
        embed_texts.append(f"{title}\n\n{embed_body}")
        embed_metas.append({
            "source": "apple_notes",
            "title": (title or path.stem)[:400],
            "path": str(path)[:500],
        })

    if embed_ids:
        chunk = 100
        console.print(f"Embedding {len(embed_ids)} notes...")
        for i in range(0, len(embed_ids), chunk):
            upsert_to_chroma(
                "documents",
                embed_ids[i : i + chunk],
                embed_texts[i : i + chunk],
                embed_metas[i : i + chunk],
            )
        with get_db() as conn:
            conn.executemany(
                "UPDATE documents SET embedded=1 WHERE id=?",
                [(eid,) for eid in embed_ids],
            )

    console.print(f"[bold green]Done.[/] {added} Apple Notes documents stored.")
    log_ingest_finish(log_id, added, 0)


def datetime_iso(p: Path) -> str:
    try:
        ts = p.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except OSError:
        return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--dir", default=None, help="Override APPLE_NOTES_EXPORT_PATH")
    args = p.parse_args()
    run(export_dir=args.dir)
