"""
ingestion/pinterest/pinterest_ingest.py

Ingest pins from Pinterest API v5 as bookmarks.

Requires PINTEREST_TOKEN in config/secrets.env (OAuth access token with appropriate scopes).

API: https://developers.pinterest.com/docs/api/v5/
"""

from __future__ import annotations

import os
import random
import sys
import time
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

BASE = "https://api.pinterest.com/v5"
SOURCE = "pinterest"
PAGE_SIZE = 25
MAX_429_RETRIES = 12


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _request_with_backoff(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Retry on 429 with exponential backoff and optional Retry-After."""
    delay = 1.0
    last: httpx.Response | None = None
    for attempt in range(MAX_429_RETRIES):
        r = client.request(method, url, params=params)
        last = r
        if r.status_code != 429:
            return r
        ra = r.headers.get("Retry-After")
        try:
            wait = float(ra) if ra else delay
        except ValueError:
            wait = delay
        wait = min(wait + random.uniform(0, 0.25), 120.0)
        console.print(
            f"  [yellow]429 rate limit — sleeping {wait:.1f}s "
            f"(attempt {attempt + 1}/{MAX_429_RETRIES})[/]"
        )
        time.sleep(wait)
        delay = min(delay * 2, 60.0)
    assert last is not None
    return last


def _get(
    client: httpx.Client,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{BASE}{path}" if path.startswith("/") else f"{BASE}/{path}"
    r = _request_with_backoff(client, "GET", url, params=params or {})
    r.raise_for_status()
    return r.json()


def _fetch_all_boards(client: httpx.Client) -> list[dict[str, Any]]:
    boards: list[dict[str, Any]] = []
    bookmark: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": PAGE_SIZE}
        if bookmark:
            params["bookmark"] = bookmark
        data = _get(client, "/boards", params=params)
        batch = data.get("items") or []
        boards.extend(batch)
        bookmark = data.get("bookmark")
        if not bookmark or not batch:
            break
    return boards


def _fetch_all_pins(client: httpx.Client, board_id: str) -> list[dict[str, Any]]:
    pins: list[dict[str, Any]] = []
    bookmark: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": PAGE_SIZE}
        if bookmark:
            params["bookmark"] = bookmark
        data = _get(client, f"/boards/{board_id}/pins", params=params)
        batch = data.get("items") or []
        pins.extend(batch)
        bookmark = data.get("bookmark")
        if not bookmark or not batch:
            break
    return pins


def _pin_destination_url(pin: dict[str, Any]) -> str:
    link = (pin.get("link") or "").strip()
    if link:
        return link
    pid = pin.get("id")
    if pid:
        return f"https://www.pinterest.com/pin/{pid}/"
    return ""


def _pin_image_url(pin: dict[str, Any]) -> str:
    media = pin.get("media") or {}
    images = media.get("images") or {}
    if not images:
        return ""
    best = ""
    best_area = 0
    for _key, meta in images.items():
        if not isinstance(meta, dict):
            continue
        u = (meta.get("url") or "").strip()
        if not u:
            continue
        w = int(meta.get("width") or 0)
        h = int(meta.get("height") or 0)
        area = w * h if w and h else 0
        if area >= best_area:
            best_area = area
            best = u
    if best:
        return best
    first = next(
        (m.get("url") for m in images.values() if isinstance(m, dict) and m.get("url")),
        "",
    )
    return (first or "")[:2000]


def _pin_media_type(pin: dict[str, Any]) -> str:
    media = pin.get("media") or {}
    return str(media.get("media_type") or pin.get("media_type") or "")


def _pin_note(pin: dict[str, Any]) -> str:
    for key in ("note", "alt_text", "board_owner_note"):
        v = pin.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:8000]
    return ""


def _embed_text(title: str, description: str, note: str, board_name: str) -> str:
    parts = [title, description, note, f"Board: {board_name}" if board_name else ""]
    return "\n\n".join(p for p in parts if p).strip()


def run() -> None:
    token = os.getenv("PINTEREST_TOKEN", "").strip()
    if not token:
        console.print(
            "[red]Set PINTEREST_TOKEN in config/secrets.env "
            "(Pinterest developer app OAuth access token).[/]"
        )
        lid = log_ingest_start("pinterest")
        log_ingest_finish(lid, 0, 0, error="PINTEREST_TOKEN not set")
        return

    log_id = log_ingest_start("pinterest")
    console.print("\n[bold]Pinterest pin ingest starting...[/]")

    try:
        with httpx.Client(
            headers=_headers(token),
            timeout=httpx.Timeout(60.0),
            follow_redirects=True,
        ) as client:
            console.print("  Fetching boards...")
            boards = _fetch_all_boards(client)
            if not boards:
                console.print("[yellow]No boards returned.[/]")
                log_ingest_finish(log_id, 0, 0)
                return

            console.print(f"  Found [bold]{len(boards)}[/] board(s)")

            embed_ids: list[str] = []
            embed_texts: list[str] = []
            embed_metas: list[dict] = []
            added = 0
            empty_tags = "[]"

            for board in track(boards, description="Boards (fetch pins & store)..."):
                board_id = str(board.get("id") or "")
                board_name = (board.get("name") or board_id or "Unnamed")[:500]
                if not board_id:
                    continue

                try:
                    pins = _fetch_all_pins(client, board_id)
                except httpx.HTTPStatusError as e:
                    console.print(
                        f"  [yellow]Skip board {board_name!r}: {e.response.status_code}[/]"
                    )
                    continue

                for pin in pins:
                    pid = pin.get("id")
                    if pid is None:
                        continue
                    pid_str = str(pid)
                    url = _pin_destination_url(pin)
                    if not url:
                        continue

                    row_id = stable_id(SOURCE, pid_str)
                    title = (pin.get("title") or url)[:4000]
                    description = (pin.get("description") or "")[:8000]
                    note = _pin_note(pin)
                    created = pin.get("created_at") or pin.get("created_at_pin") or ""
                    cover = _pin_image_url(pin)
                    media_type = _pin_media_type(pin)

                    embed_body = _embed_text(title, description, note, board_name)
                    if media_type:
                        embed_body = f"{embed_body}\n\nMedia: {media_type}".strip()

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
                                url,
                                title,
                                board_name,
                                created,
                                description,
                                empty_tags,
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
                                "url": url[:500],
                                "folder": board_name[:200],
                                "added_date": created[:10] if created else "",
                            }
                        )

        if embed_ids:
            console.print(f"Embedding {len(embed_ids)} new pin(s) into Chroma...")
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

        console.print(
            f"[bold green]Done.[/] {added} new Pinterest pin(s) (INSERT OR IGNORE)."
        )
        log_ingest_finish(log_id, added, 0)
    except httpx.HTTPStatusError as e:
        msg = f"{e.response.status_code} {e.response.text[:300]}"
        console.print(f"[red]Pinterest API error: {msg}[/]")
        log_ingest_finish(log_id, 0, 0, error=msg)
        raise
    except Exception as e:
        console.print(f"[red]Pinterest ingest failed: {e}[/]")
        log_ingest_finish(log_id, 0, 0, error=str(e))
        raise


if __name__ == "__main__":
    run()
