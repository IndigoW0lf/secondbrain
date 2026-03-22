"""
scheduler/jobs.py

Runs all ingest pipelines on a schedule.
Start this as a background service on your VPS.

Usage:
  python scheduler/jobs.py

  # Or as a systemd service (recommended for VPS):
  # See scheduler/secondbrain.service for the unit file
"""

import sys
import os
import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from rich.console import Console

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

console = Console()
logging.basicConfig(level=logging.INFO)


def run_financial():
    console.print(f"\n[bold blue][{datetime.now():%H:%M}] Running financial ingest...[/]")
    try:
        from ingestion.financial.plaid_ingest import run
        run(days_back=7)
    except Exception as e:
        console.print(f"[red]Financial ingest failed: {e}[/]")


def run_email():
    console.print(f"\n[bold blue][{datetime.now():%H:%M}] Running email ingest...[/]")
    try:
        from ingestion.email.gmail_ingest import run
        run(max_emails=50)
    except Exception as e:
        console.print(f"[red]Email ingest failed: {e}[/]")


def run_calendar():
    console.print(f"\n[bold blue][{datetime.now():%H:%M}] Running calendar ingest...[/]")
    try:
        from ingestion.calendar.gcal_ingest import run
        run(days_back=365, days_forward=365)
    except Exception as e:
        console.print(f"[red]Calendar ingest failed: {e}[/]")


def run_amazon():
    console.print(f"\n[bold blue][{datetime.now():%H:%M}] Running Amazon order parse...[/]")
    try:
        from ingestion.amazon.amazon_ingest import run
        run()
    except Exception as e:
        console.print(f"[red]Amazon ingest failed: {e}[/]")


def run_gdrive():
    console.print(f"\n[bold blue][{datetime.now():%H:%M}] Running Google Drive ingest...[/]")
    try:
        from ingestion.files.gdrive_ingest import run
        run(max_files=300)
    except Exception as e:
        console.print(f"[red]Google Drive ingest failed: {e}[/]")


def run_apple_notes():
    console.print(f"\n[bold blue][{datetime.now():%H:%M}] Running Apple Notes ingest...[/]")
    try:
        from ingestion.notes.apple_notes_ingest import run
        run()
    except Exception as e:
        console.print(f"[red]Apple Notes ingest failed: {e}[/]")


def run_raindrop():
    console.print(f"\n[bold blue][{datetime.now():%H:%M}] Running Raindrop ingest...[/]")
    try:
        from ingestion.raindrop.raindrop_ingest import run
        run()
    except Exception as e:
        console.print(f"[red]Raindrop ingest failed: {e}[/]")


def run_all():
    """Full sync — run weekly for larger historical pulls."""
    console.print(f"\n[bold green][{datetime.now():%H:%M}] Running full sync...[/]")
    run_financial()
    run_email()
    run_calendar()
    run_amazon()
    run_gdrive()
    run_apple_notes()
    run_raindrop()


if __name__ == "__main__":
    scheduler = BlockingScheduler(timezone="America/Chicago")

    scheduler.add_job(run_financial, CronTrigger(hour="7,19", minute="0"))
    scheduler.add_job(run_email, CronTrigger(hour="*/4", minute="30"))
    scheduler.add_job(run_calendar, CronTrigger(hour="7,19", minute="15"))
    scheduler.add_job(run_amazon, CronTrigger(hour="8", minute="45"))
    scheduler.add_job(run_gdrive, CronTrigger(hour="3", minute="0"))
    scheduler.add_job(run_apple_notes, CronTrigger(day_of_week="sun", hour="4", minute="0"))
    scheduler.add_job(run_raindrop, IntervalTrigger(hours=6))
    scheduler.add_job(run_all, CronTrigger(day_of_week="sun", hour="2", minute="0"))

    console.print("[bold green]Scheduler started.[/]")
    console.print("Jobs:")
    console.print("  Financial: 7am + 7pm daily")
    console.print("  Email:     every 4 hours")
    console.print("  Calendar: 7:15am + 7:15pm daily")
    console.print("  Amazon:    8:45am daily")
    console.print("  GDrive:    3am daily")
    console.print("  Apple Notes: Sunday 4am")
    console.print("  Raindrop:    every 6 hours")
    console.print("  Full sync: Sunday 2am")
    console.print("\nPress Ctrl+C to stop.\n")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        console.print("\n[yellow]Scheduler stopped.[/]")
