"""
ingestion/calendar/gcal_ingest.py

Sync Google Calendar events into SQLite and ChromaDB (collection "calendar").

OAuth: same as Gmail/Drive — ingestion.google_common (GOOGLE_CREDENTIALS_PATH,
GOOGLE_TOKEN_PATH, refresh flow). Scope includes calendar.readonly.

Enable Calendar API in Google Cloud Console.
"""

import os
import sys
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import track
from rich.table import Table

load_dotenv("config/secrets.env")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from ingestion.google_common import get_google_credentials
from storage.store import (
    get_db,
    upsert_to_chroma,
    log_ingest_start,
    log_ingest_finish,
    stable_id,
)

console = Console()


def get_calendar_service():
    creds = get_google_credentials()
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def list_accessible_calendars(service, only_id: str | None = None) -> list[dict]:
    """Each item: {id, summary}. If only_id is set, return just that calendar."""
    if only_id:
        try:
            meta = service.calendars().get(calendarId=only_id).execute()
            return [
                {
                    "id": only_id,
                    "summary": meta.get("summary", only_id),
                }
            ]
        except HttpError as e:
            console.print(f"[red]Calendar {only_id!r}: {e}[/]")
            raise

    out: list[dict] = []
    page_token = None
    while True:
        resp = (
            service.calendarList()
            .list(pageToken=page_token, maxResults=250)
            .execute()
        )
        for item in resp.get("items", []):
            out.append(
                {
                    "id": item["id"],
                    "summary": item.get("summary", item["id"]),
                }
            )
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def fetch_events(
    service,
    calendar_id: str,
    time_min: str,
    time_max: str,
) -> list[dict]:
    events: list[dict] = []
    page_token = None
    while True:
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                pageToken=page_token,
            )
            .execute()
        )
        events.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return events


def _event_times(ev: dict) -> tuple[str, str, int]:
    """Return (start_iso, end_iso, all_day)."""
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    if start.get("dateTime"):
        s = start["dateTime"]
        e = end.get("dateTime") or s
        return s, e, 0
    sd = start.get("date", "")
    ed = end.get("date", sd)
    return f"{sd}T00:00:00", f"{ed}T00:00:00", 1


def run(
    days_back: int = 365,
    days_forward: int = 365,
    calendar_id: str | None = None,
):
    """
    Pull events from all calendars (or one if calendar_id is set).
    Stores rows with INSERT OR REPLACE; Chroma metadata: source, calendar_id, start_dt, title.
    """
    log_id = log_ingest_start("gcal")
    console.print("\n[bold]Google Calendar ingest starting...[/]")

    try:
        service = get_calendar_service()
        now = datetime.now(timezone.utc)
        time_min = (now - timedelta(days=days_back)).isoformat()
        time_max = (now + timedelta(days=days_forward)).isoformat()

        console.print(
            f"  Window: [cyan]{time_min[:10]}[/] → [cyan]{time_max[:10]}[/] "
            f"([dim]{days_back}d back, {days_forward}d forward[/])"
        )

        calendars = list_accessible_calendars(service, only_id=calendar_id)
        if not calendars:
            console.print("[yellow]No calendars found.[/]")
            log_ingest_finish(log_id, 0, 0)
            return

        console.print(f"  Calendars to scan: [bold]{len(calendars)}[/]")

        workload: list[tuple[dict, dict]] = []
        skipped: list[tuple[str, str]] = []

        for cal in track(calendars, description="Fetching events per calendar..."):
            try:
                evs = fetch_events(service, cal["id"], time_min, time_max)
            except HttpError as e:
                skipped.append((cal.get("summary", cal["id"]), str(e)))
                continue
            for ev in evs:
                workload.append((cal, ev))

        if skipped:
            for name, err in skipped:
                console.print(f"  [yellow]Skipped[/] {name}: {err[:120]}")

        if not workload:
            console.print("[yellow]No events in window.[/]")
            _print_summary_table(calendars, defaultdict(int))
            log_ingest_finish(log_id, 0, 0)
            return

        console.print(f"  Total event instances to store: [bold]{len(workload)}[/]")

        counts: dict[str, int] = defaultdict(int)
        id_by_cal: dict[str, str] = {c["id"]: c["summary"] for c in calendars}

        embed_ids: list[str] = []
        embed_texts: list[str] = []
        embed_metas: list[dict] = []

        for cal, ev in track(workload, description="Storing events..."):
            eid = ev.get("id")
            if not eid:
                continue
            cal_id = cal["id"]
            row_id = stable_id(cal_id, eid)

            start_dt, end_dt, all_day = _event_times(ev)
            title = ev.get("summary") or "(no title)"
            description = ev.get("description") or ""
            location = ev.get("location") or ""
            attendees = json.dumps(
                [a.get("email", "") for a in ev.get("attendees", []) if a.get("email")]
            )
            status = ev.get("status") or ""
            recurrence = ev.get("recurrence")
            recurrence_s = json.dumps(recurrence) if recurrence else ""

            with get_db() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO calendar_events
                        (id, calendar_id, title, description, location, start_dt, end_dt,
                         all_day, attendees, status, recurrence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id,
                        cal_id,
                        title,
                        description,
                        location,
                        start_dt,
                        end_dt,
                        all_day,
                        attendees,
                        status,
                        recurrence_s,
                    ),
                )

            counts[cal_id] += 1

            embed_text = f"{title}\n{location}\n{description}".strip()
            if len(embed_text) > 8000:
                embed_text = embed_text[:8000]

            embed_ids.append(row_id)
            embed_texts.append(embed_text)
            embed_metas.append(
                {
                    "source": "gcal",
                    "calendar_id": cal_id[:500],
                    "start_dt": start_dt[:64],
                    "title": title[:500],
                }
            )

        if embed_ids:
            chunk = 200
            console.print(f"Embedding {len(embed_ids)} events into Chroma...")
            for i in range(0, len(embed_ids), chunk):
                upsert_to_chroma(
                    "calendar",
                    embed_ids[i : i + chunk],
                    embed_texts[i : i + chunk],
                    embed_metas[i : i + chunk],
                )

        _print_summary_table(
            calendars,
            counts,
            id_to_summary=id_by_cal,
        )

        console.print(
            f"\n[bold green]Done.[/] {len(embed_ids)} calendar event(s) upserted."
        )
        log_ingest_finish(log_id, len(embed_ids), 0)
    except Exception as e:
        console.print(f"[red]gcal ingest failed: {e}[/]")
        log_ingest_finish(log_id, 0, 0, error=str(e))
        raise


def _print_summary_table(
    calendars: list[dict],
    counts: dict[str, int],
    id_to_summary: dict[str, str] | None = None,
) -> None:
    id_to_summary = id_to_summary or {c["id"]: c["summary"] for c in calendars}
    table = Table(title="Events pulled per calendar")
    table.add_column("Calendar", style="bold")
    table.add_column("Calendar ID", overflow="fold", style="dim")
    table.add_column("Events", justify="right")

    cal_ids_ordered = sorted(
        {c["id"] for c in calendars},
        key=lambda cid: (-counts.get(cid, 0), id_to_summary.get(cid, cid)),
    )
    for cid in cal_ids_ordered:
        table.add_row(
            id_to_summary.get(cid, cid),
            cid,
            str(counts.get(cid, 0)),
        )

    console.print()
    console.print(table)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Ingest Google Calendar events (all calendars by default).")
    p.add_argument(
        "--days-back",
        type=int,
        default=365,
        help="Days before now to include (default: 365)",
    )
    p.add_argument(
        "--days-forward",
        type=int,
        default=365,
        help="Days after now to include (default: 365)",
    )
    p.add_argument(
        "--calendar",
        default=None,
        metavar="ID",
        help="Only this calendar ID (e.g. primary or email address); default: all accessible",
    )
    args = p.parse_args()
    run(
        days_back=args.days_back,
        days_forward=args.days_forward,
        calendar_id=args.calendar,
    )
