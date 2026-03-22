"""
ingestion/files/gdrive_ingest.py

Ingest Google Docs and plain-text files from Google Drive.
Stores full text in documents.body_text, embeds into ChromaDB collection "documents".

Uses ingestion.google_common OAuth (Drive API must be enabled in Cloud Console).
"""

import io
import os
import sys
import re
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track

load_dotenv("config/secrets.env")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from ingestion.google_common import get_google_credentials
from storage.store import get_db, upsert_to_chroma, log_ingest_start, log_ingest_finish, stable_id

console = Console()

MIME_QUERY = (
    "mimeType = 'application/vnd.google-apps.document' "
    "or mimeType = 'text/plain' "
    "or mimeType = 'text/markdown'"
)


def get_drive_service():
    creds = get_google_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _download_media(service, request) -> bytes:
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue()


def fetch_file_text(service, file_id: str, mime_type: str) -> str:
    if mime_type == "application/vnd.google-apps.document":
        req = service.files().export_media(fileId=file_id, mimeType="text/plain")
    else:
        req = service.files().get_media(fileId=file_id)
    raw = _download_media(service, req)
    return raw.decode("utf-8", errors="replace")


def run(max_files: int = 300, folder_id: str | None = None):
    log_id = log_ingest_start("gdrive")
    console.print("\n[bold]Google Drive ingest starting...[/]")

    folder_id = folder_id or os.getenv("GDRIVE_FOLDER_ID") or None

    try:
        service = get_drive_service()
        q_parts = [f"({MIME_QUERY})", "trashed = false"]
        if folder_id:
            q_parts.append(f"'{folder_id}' in parents")
        q = " and ".join(q_parts)

        files: list[dict] = []
        page_token = None
        while len(files) < max_files:
            res = service.files().list(
                q=q,
                pageSize=min(100, max_files - len(files)),
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, createdTime)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            batch = res.get("files", [])
            files.extend(batch)
            page_token = res.get("nextPageToken")
            if not page_token or len(files) >= max_files:
                break

        files = files[:max_files]
        if not files:
            console.print("[yellow]No matching Drive files found.[/]")
            log_ingest_finish(log_id, 0, 0)
            return

        console.print(f"  Processing {len(files)} files")

        embed_ids, embed_texts, embed_metas = [], [], []
        added = 0

        for fmeta in track(files, description="Downloading & storing..."):
            fid = fmeta["id"]
            name = fmeta.get("name") or "untitled"
            mime = fmeta.get("mimeType") or ""
            path = f"https://drive.google.com/file/d/{fid}/view"
            modified = fmeta.get("modifiedTime") or fmeta.get("createdTime") or ""
            created = fmeta.get("createdTime") or ""

            try:
                body = fetch_file_text(service, fid, mime)
            except Exception as e:
                console.print(f"  [yellow]Skip {name}: {e}[/]")
                continue

            body = body.strip()
            if not body:
                continue

            doc_id = stable_id("gdrive", fid)
            wc = len(body.split())

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
                        "gdrive",
                        name,
                        path,
                        mime,
                        created,
                        modified,
                        wc,
                        body[:500000],
                    ),
                )
            added += 1

            embed_body = body[:12000] if len(body) > 12000 else body
            embed_ids.append(doc_id)
            embed_texts.append(f"{name}\n\n{embed_body}")
            embed_metas.append({
                "source": "gdrive",
                "title": name[:400],
                "mime_type": mime[:80],
                "path": path[:500],
            })

        if embed_ids:
            chunk = 100
            console.print(f"Embedding {len(embed_ids)} documents...")
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

        console.print(f"[bold green]Done.[/] {added} Drive documents stored.")
        log_ingest_finish(log_id, added, 0)
    except Exception as e:
        console.print(f"[red]gdrive ingest failed: {e}[/]")
        log_ingest_finish(log_id, 0, 0, error=str(e))
        raise


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--max", type=int, default=300)
    p.add_argument("--folder", default=None, help="Drive folder ID to scope search")
    args = p.parse_args()
    run(max_files=args.max, folder_id=args.folder)
