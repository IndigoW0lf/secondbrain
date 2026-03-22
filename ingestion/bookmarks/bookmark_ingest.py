"""
ingestion/bookmarks/bookmark_ingest.py

Ingests browser bookmarks from exported HTML files.
Works with Chrome, Firefox, Safari, and Edge exports.

EXPORT INSTRUCTIONS:

  Chrome/Edge:
    Bookmarks menu → Bookmark manager → ⋮ → Export bookmarks
    Saves as: bookmarks_MM_DD_YY.html

  Firefox:
    Bookmarks → Manage bookmarks → Import and Backup → Export Bookmarks to HTML
    Saves as: bookmarks.html

  Safari:
    File → Export Bookmarks
    Saves as: Safari Bookmarks.html

Run:
  python ingestion/bookmarks/bookmark_ingest.py --file ~/Downloads/bookmarks.html
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from rich.console import Console
from rich.progress import track

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from storage.store import (
    get_db,
    log_ingest_finish,
    log_ingest_start,
    stable_id,
    upsert_to_chroma,
)

console = Console()


def parse_bookmarks_html(html_path: str) -> list[dict]:
    """
    Parse Netscape Bookmark File Format (used by all major browsers).

    Chrome/Firefox often structure folders as:
      <DL>
        <DT><H3>Folder name</H3>
        <DL> ... bookmarks ... </DL>   <!-- sibling of DT, NOT nested inside DT -->
    So we look for the following <dl> as a *sibling* after the <dt>, not only inside it.
    After walking that <dl>, we skip it in the parent iteration so we don't process it twice.
    """
    soup = BeautifulSoup(
        Path(html_path).read_text(encoding="utf-8", errors="replace"),
        "html.parser",
    )
    bookmarks: list[dict] = []

    def walk(element, folder_path: list[str]) -> None:
        children = [c for c in element.children if hasattr(c, "name") and c.name is not None]
        i = 0
        while i < len(children):
            tag = children[i]

            if tag.name == "dt":
                inner = tag.find(["a", "h3"], recursive=False)
                if inner is None:
                    inner = tag.find(["a", "h3"])

                if inner and inner.name == "a":
                    url = inner.get("href", "")
                    if url and not url.startswith("javascript:"):
                        add_date = inner.get("add_date", "")
                        try:
                            ts = (
                                datetime.fromtimestamp(
                                    int(add_date), tz=timezone.utc
                                ).isoformat()
                                if add_date
                                else ""
                            )
                        except (ValueError, TypeError, OSError):
                            ts = ""
                        bookmarks.append(
                            {
                                "url": url,
                                "title": inner.get_text(strip=True) or url,
                                "folder": " / ".join(folder_path)
                                if folder_path
                                else "Unsorted",
                                "added_date": ts,
                            }
                        )

                elif inner and inner.name == "h3":
                    folder_name = inner.get_text(strip=True)
                    # Sibling <dl> after this <dt> (Chrome/Firefox layout)
                    found_sibling_dl = False
                    j = i + 1
                    while j < len(children):
                        sibling = children[j]
                        if getattr(sibling, "name", None) == "dl":
                            walk(sibling, folder_path + [folder_name])
                            i = j
                            found_sibling_dl = True
                            break
                        j += 1
                    # Fallback: <dl> nested inside <dt> (Safari / some exports)
                    if not found_sibling_dl:
                        nested = tag.find("dl", recursive=False)
                        if nested is None:
                            nested = tag.find("dl")
                        if nested is not None:
                            walk(nested, folder_path + [folder_name])

            elif tag.name == "dl":
                walk(tag, folder_path)

            i += 1

    root = soup.find("dl")
    if root:
        walk(root, [])

    return bookmarks


def run(file_path: str, source: str = "browser"):
    log_id = log_ingest_start("bookmarks")
    console.print(f"\n[bold]Bookmark ingest from {file_path}[/]")

    bookmarks = parse_bookmarks_html(file_path)
    console.print(f"  Parsed {len(bookmarks)} bookmarks")

    with get_db() as conn:
        existing_urls = set(
            row[0] for row in conn.execute("SELECT url FROM bookmarks").fetchall()
        )

    new_bookmarks = [b for b in bookmarks if b["url"] not in existing_urls]
    console.print(
        f"  {len(new_bookmarks)} new, {len(bookmarks) - len(new_bookmarks)} already stored"
    )

    added = 0
    embed_ids, embed_texts, embed_metas = [], [], []

    for bm in track(new_bookmarks, description="Storing bookmarks..."):
        bm_id = stable_id(bm["url"])
        with get_db() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO bookmarks
                    (id, url, title, folder, added_date, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    bm_id,
                    bm["url"],
                    bm["title"],
                    bm["folder"],
                    bm["added_date"],
                    source,
                ),
            )
        added += 1

        embed_text = f"{bm['title']}\n{bm['url']}\nFolder: {bm['folder']}"
        embed_ids.append(bm_id)
        embed_texts.append(embed_text)
        embed_metas.append(
            {
                "source": source,
                "title": bm["title"][:200],
                "url": bm["url"][:500],
                "folder": bm["folder"][:200],
                "added_date": bm["added_date"][:10] if bm["added_date"] else "",
            }
        )

    if embed_ids:
        console.print(f"Embedding {len(embed_ids)} bookmarks...")
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

    console.print(f"[bold green]Done.[/] {added} bookmarks stored and embedded.")
    log_ingest_finish(log_id, added, 0)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--file", required=True, help="Path to exported bookmarks HTML file"
    )
    parser.add_argument(
        "--source",
        default="browser",
        help="browser | chrome | firefox | safari",
    )
    args = parser.parse_args()
    run(file_path=args.file, source=args.source)
