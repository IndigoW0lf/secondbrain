"""
ingestion/raindrop/raindrop_ingest.py

Ingest bookmarks from Raindrop.io REST API v1.

Requires RAINDROP_TOKEN in config/secrets.env (Bearer token from Raindrop settings → Integrations).

API: https://developer.raindrop.io/
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track

load_dotenv("config/secrets.env")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from storage.store import (
    get_db,
    log_ingest_finish,
    log_ingest_start,
    stable_id,
    upsert_to_chroma,
)

console = Console()

BASE = "https://api.raindrop.io/rest/v1"
SOURCE = "raindrop"
PER_PAGE = 50


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _collection_titles(client: httpx.Client) -> dict[int, str]:
    """Map collection id -> title for folder names."""
    r = client.get(f"{BASE}/collections")
    r.raise_for_status()
    data = r.json()
    out: dict[int, str] = {}
    for c in data.get("items", []):
        cid = c.get("_id")
        if cid is not None:
            out[int(cid)] = c.get("title") or str(cid)
    out[-1] = "Unsorted"
    return out


def _paginate_raindrops(
    client: httpx.Client,
    collection_id: int,
    *,
    nested: bool = False,
) -> list[dict[str, Any]]:
    """Fetch all raindrops for a collection with pagination."""
    items: list[dict[str, Any]] = []
    page = 0
    while True:
        params: dict[str, Any] = {
            "perpage": PER_PAGE,
            "page": page,
            "sort": "-created",
        }
        if nested:
            params["nested"] = "true"
        r = client.get(f"{BASE}/raindrops/{collection_id}", params=params)
        r.raise_for_status()
        data = r.json()
        batch = data.get("items") or []
        if not batch:
            break
        items.extend(batch)
        if len(batch) < PER_PAGE:
            break
        page += 1
    return items


def _merge_all_raindrops(client: httpx.Client) -> list[dict[str, Any]]:
    """
    All bookmarks from all collections including Unsorted.
    Uses collection 0 (all except Trash) with nested collections, plus -1 (Unsorted)
    to match Raindrop semantics; dedupe by _id.
    """
    merged: dict[int, dict[str, Any]] = {}
    for coll_id, nested in ((0, True), (-1, False)):
        for item in _paginate_raindrops(client, coll_id, nested=nested):
            rid = item.get("_id")
            if rid is not None:
                merged[int(rid)] = item
    return list(merged.values())


def _folder_name(
    item: dict[str, Any],
    col_titles: dict[int, str],
) -> str:
    col = item.get("collection") or {}
    if isinstance(col, dict):
        cid = col.get("$id")
        if cid is not None:
            cid = int(cid)
            return col.get("title") or col_titles.get(cid, str(cid))
    return "Unsorted"


def _tags_json(item: dict[str, Any]) -> str:
    tags = item.get("tags")
    if tags is None:
        return "[]"
    if isinstance(tags, list):
        return json.dumps(tags, ensure_ascii=False)
    return json.dumps([str(tags)], ensure_ascii=False)


def _fetch_highlights(client: httpx.Client, raindrop_id: int) -> list[str]:
    """GET /raindrop/{id}/highlights; fall back to empty on 404."""
    url = f"{BASE}/raindrop/{raindrop_id}/highlights"
    try:
        r = client.get(url)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError:
        return []

    texts: list[str] = []
    for key in ("items", "highlights"):
        for h in data.get(key) or []:
            if isinstance(h, dict):
                t = h.get("text") or h.get("content")
                if t:
                    texts.append(str(t).strip())
    return texts


def _build_embed_text(
    title: str,
    excerpt: str,
    note: str,
    tags_json: str,
    highlights: list[str],
) -> str:
    parts = [title or "", excerpt or "", note or ""]
    try:
        tags = json.loads(tags_json) if tags_json else []
        if tags:
            parts.append("Tags: " + ", ".join(str(t) for t in tags))
    except json.JSONDecodeError:
        pass
    if highlights:
        parts.append("Highlights:\n" + "\n".join(highlights))
    return "\n\n".join(p for p in parts if p).strip()


def run() -> None:
    token = os.getenv("RAINDROP_TOKEN", "").strip()
    if not token:
        console.print(
            "[red]Set RAINDROP_TOKEN in config/secrets.env "
            "(Raindrop → Settings → Integrations → Create app token).[/]"
        )
        lid = log_ingest_start("raindrop")
        log_ingest_finish(lid, 0, 0, error="RAINDROP_TOKEN not set")
        return

    log_id = log_ingest_start("raindrop")
    console.print("\n[bold]Raindrop.io bookmark ingest starting...[/]")

    try:
        with httpx.Client(
            headers=_headers(token),
            timeout=httpx.Timeout(60.0),
            follow_redirects=True,
        ) as client:
            col_titles = _collection_titles(client)
            items = _merge_all_raindrops(client)

            if not items:
                console.print("[yellow]No bookmarks returned from Raindrop.[/]")
                log_ingest_finish(log_id, 0, 0)
                return

            console.print(f"  Fetched [bold]{len(items)}[/] unique bookmark(s)")

            embed_ids: list[str] = []
            embed_texts: list[str] = []
            embed_metas: list[dict] = []
            added = 0

            for item in track(items, description="Storing & embedding..."):
                rid = item.get("_id")
                if rid is None:
                    continue
                rid = int(rid)
                link = (item.get("link") or "").strip()
                if not link:
                    continue

                row_id = stable_id(SOURCE, str(rid))
                title = (item.get("title") or link)[:4000]
                excerpt = (item.get("excerpt") or "")[:8000]
                note = (item.get("note") or "")[:8000]
                cover = (item.get("cover") or "")[:2000]
                tags_s = _tags_json(item)
                folder = _folder_name(item, col_titles)
                created = item.get("created") or item.get("lastUpdate") or ""

                highlights = _fetch_highlights(client, rid)
                embed_body = _build_embed_text(
                    title, excerpt, note, tags_s, highlights
                )

                with get_db() as conn:
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO bookmarks
                            (id, url, title, folder, added_date, description, tags,
                             note, cover_url, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row_id,
                            link,
                            title,
                            folder,
                            created,
                            excerpt,
                            tags_s,
                            note,
                            cover,
                            SOURCE,
                        ),
                    )
                    if cur.rowcount:
                        added += 1
                        embed_ids.append(row_id)
                        embed_texts.append(embed_body or title)
                        embed_metas.append(
                            {
                                "source": SOURCE,
                                "title": title[:200],
                                "url": link[:500],
                                "folder": folder[:200],
                                "added_date": created[:10] if created else "",
                            }
                        )

        if embed_ids:
            console.print(f"Embedding {len(embed_ids)} new bookmark(s) into Chroma...")
            chunk = 200
            for i in range(0, len(embed_ids), chunk):
                upsert_to_chroma(
                    "bookmarks",
                    embed_ids[i : i + chunk],
                    embed_texts[i : i + chunk],
                    embed_metas[i : i + chunk],
                )
            with get_db() as conn:
                conn.executemany(
                    "UPDATE bookmarks SET embedded=1 WHERE id=?",
                    [(eid,) for eid in embed_ids],
                )

        console.print(f"[bold green]Done.[/] {added} new bookmark(s) inserted (INSERT OR IGNORE).")
        log_ingest_finish(log_id, added, 0)
    except httpx.HTTPStatusError as e:
        msg = f"{e.response.status_code} {e.response.text[:200]}"
        console.print(f"[red]Raindrop API error: {msg}[/]")
        log_ingest_finish(log_id, 0, 0, error=msg)
        raise
    except Exception as e:
        console.print(f"[red]Raindrop ingest failed: {e}[/]")
        log_ingest_finish(log_id, 0, 0, error=str(e))
        raise


if __name__ == "__main__":
    run()
