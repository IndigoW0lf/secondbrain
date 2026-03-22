"""
storage/init_db.py

Initialize the SQLite database with all tables.
Run once on setup, safe to re-run (uses IF NOT EXISTS).
"""

import sqlite3
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("config/secrets.env")
DB_PATH = os.getenv("DB_PATH", "storage/secondbrain.db")


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after initial schema."""
    def _cols(table: str) -> set[str]:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    if "documents" in [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
    ).fetchall()]:
        if "body_text" not in _cols("documents"):
            conn.execute("ALTER TABLE documents ADD COLUMN body_text TEXT")

    if "bookmarks" in [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bookmarks'"
    ).fetchall()]:
        if "note" not in _cols("bookmarks"):
            conn.execute("ALTER TABLE bookmarks ADD COLUMN note TEXT")
        if "cover_url" not in _cols("bookmarks"):
            conn.execute("ALTER TABLE bookmarks ADD COLUMN cover_url TEXT")


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")  # better concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")
    c = conn.cursor()

    # ------------------------------------------------------------------ #
    # FINANCIAL                                                            #
    # ------------------------------------------------------------------ #
    c.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id              TEXT PRIMARY KEY,   -- Plaid account_id
            institution     TEXT NOT NULL,
            name            TEXT NOT NULL,
            type            TEXT NOT NULL,      -- depository | credit | investment
            subtype         TEXT,               -- checking | savings | credit card
            mask            TEXT,               -- last 4 digits
            current_balance REAL,
            available_balance REAL,
            currency        TEXT DEFAULT 'USD',
            last_synced     TEXT                -- ISO datetime
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id              TEXT PRIMARY KEY,   -- Plaid transaction_id
            account_id      TEXT NOT NULL REFERENCES accounts(id),
            date            TEXT NOT NULL,      -- YYYY-MM-DD
            amount          REAL NOT NULL,      -- positive = debit, negative = credit
            description     TEXT NOT NULL,      -- raw merchant name
            merchant_name   TEXT,               -- cleaned by Plaid
            category        TEXT,               -- Plaid primary category
            subcategory     TEXT,               -- Plaid detailed category
            pending         INTEGER DEFAULT 0,  -- 0 | 1
            location_city   TEXT,
            location_state  TEXT,
            payment_channel TEXT,               -- online | in store | other
            logo_url        TEXT,
            ingested_at     TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
        CREATE INDEX IF NOT EXISTS idx_txn_account ON transactions(account_id);
        CREATE INDEX IF NOT EXISTS idx_txn_merchant ON transactions(merchant_name);
        CREATE INDEX IF NOT EXISTS idx_txn_category ON transactions(category);
    """)

    # ------------------------------------------------------------------ #
    # EMAIL                                                                #
    # ------------------------------------------------------------------ #
    c.executescript("""
        CREATE TABLE IF NOT EXISTS emails (
            id              TEXT PRIMARY KEY,   -- Gmail message id
            thread_id       TEXT,
            from_address    TEXT,
            from_name       TEXT,
            to_address      TEXT,
            subject         TEXT,
            date            TEXT,               -- ISO datetime
            snippet         TEXT,               -- Gmail's 200-char preview
            labels          TEXT,               -- JSON array
            has_attachment  INTEGER DEFAULT 0,
            body_text       TEXT,               -- plain text body
            ingested_at     TEXT DEFAULT (datetime('now')),
            embedded        INTEGER DEFAULT 0   -- 1 when added to ChromaDB
        );

        CREATE INDEX IF NOT EXISTS idx_email_date ON emails(date);
        CREATE INDEX IF NOT EXISTS idx_email_from ON emails(from_address);
        CREATE INDEX IF NOT EXISTS idx_email_embedded ON emails(embedded);
    """)

    # ------------------------------------------------------------------ #
    # CALENDAR                                                             #
    # ------------------------------------------------------------------ #
    c.executescript("""
        CREATE TABLE IF NOT EXISTS calendar_events (
            id              TEXT PRIMARY KEY,   -- Google event id
            calendar_id     TEXT,
            title           TEXT,
            description     TEXT,
            location        TEXT,
            start_dt        TEXT,               -- ISO datetime
            end_dt          TEXT,
            all_day         INTEGER DEFAULT 0,
            attendees       TEXT,               -- JSON array of emails
            status          TEXT,               -- confirmed | tentative | cancelled
            recurrence      TEXT,               -- RRULE string if recurring
            ingested_at     TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_event_start ON calendar_events(start_dt);
    """)

    # ------------------------------------------------------------------ #
    # AMAZON ORDERS                                                        #
    # ------------------------------------------------------------------ #
    c.executescript("""
        CREATE TABLE IF NOT EXISTS amazon_orders (
            id              TEXT PRIMARY KEY,   -- order number
            order_date      TEXT,
            total_amount    REAL,
            currency        TEXT DEFAULT 'USD',
            status          TEXT,
            items           TEXT,               -- JSON array of {name, qty, price}
            shipping_address TEXT,
            source_email_id TEXT REFERENCES emails(id),
            ingested_at     TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_amazon_date ON amazon_orders(order_date);
    """)

    # ------------------------------------------------------------------ #
    # AI CHAT HISTORY                                                      #
    # ------------------------------------------------------------------ #
    c.executescript("""
        CREATE TABLE IF NOT EXISTS ai_conversations (
            id              TEXT PRIMARY KEY,
            source          TEXT NOT NULL,      -- claude | chatgpt | gemini
            title           TEXT,
            created_at      TEXT,
            updated_at      TEXT,
            message_count   INTEGER,
            ingested_at     TEXT DEFAULT (datetime('now')),
            embedded        INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS ai_messages (
            id              TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES ai_conversations(id),
            role            TEXT NOT NULL,      -- user | assistant
            content         TEXT NOT NULL,
            created_at      TEXT,
            token_count     INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_aimsg_conv ON ai_messages(conversation_id);
        CREATE INDEX IF NOT EXISTS idx_aimsg_role ON ai_messages(role);
    """)

    # ------------------------------------------------------------------ #
    # DOCUMENTS / FILES                                                    #
    # ------------------------------------------------------------------ #
    c.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id              TEXT PRIMARY KEY,   -- hash of path or drive id
            source          TEXT NOT NULL,      -- local | gdrive | apple_notes | bookmark
            title           TEXT,
            path            TEXT,               -- local path or URL
            mime_type       TEXT,
            created_at      TEXT,
            modified_at     TEXT,
            word_count      INTEGER,
            body_text       TEXT,               -- full text for search / embedding
            tags            TEXT,               -- JSON array
            ingested_at     TEXT DEFAULT (datetime('now')),
            embedded        INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_doc_source ON documents(source);
        CREATE INDEX IF NOT EXISTS idx_doc_modified ON documents(modified_at);
        CREATE INDEX IF NOT EXISTS idx_doc_embedded ON documents(embedded);
    """)

    # ------------------------------------------------------------------ #
    # BOOKMARKS                                                            #
    # ------------------------------------------------------------------ #
    c.executescript("""
        CREATE TABLE IF NOT EXISTS bookmarks (
            id              TEXT PRIMARY KEY,   -- hash of url or source-specific id
            url             TEXT NOT NULL UNIQUE,
            title           TEXT,
            folder          TEXT,               -- bookmark folder path
            added_date      TEXT,
            description     TEXT,               -- page meta / excerpt
            tags            TEXT,               -- JSON array
            note            TEXT,               -- Raindrop notes, etc.
            cover_url       TEXT,
            source          TEXT DEFAULT 'browser', -- browser | pocket | raindrop
            ingested_at     TEXT DEFAULT (datetime('now')),
            embedded        INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_bookmark_added ON bookmarks(added_date);
        CREATE INDEX IF NOT EXISTS idx_bookmark_folder ON bookmarks(folder);
    """)

    # ------------------------------------------------------------------ #
    # INGEST LOG (track what ran when)                                     #
    # ------------------------------------------------------------------ #
    c.executescript("""
        CREATE TABLE IF NOT EXISTS ingest_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT NOT NULL,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            records_added   INTEGER DEFAULT 0,
            records_updated INTEGER DEFAULT 0,
            status          TEXT DEFAULT 'running', -- running | success | error
            error_message   TEXT
        );
    """)

    conn.commit()
    _migrate(conn)
    conn.commit()
    conn.close()
    print(f"✅ Database initialized at {DB_PATH}")


if __name__ == "__main__":
    init_db()
